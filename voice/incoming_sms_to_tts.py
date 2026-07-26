#!/usr/bin/env python3
"""Receive short UCS2 SMS messages from the trusted contact and hand them to TTS.

The read-only mode is the deployment gate: it lists and decodes messages but never
creates IPC requests or deletes modem storage. Service mode deletes a message only
after the voice assistant reports that hardware TTS reached its completed state.
"""

import argparse
import contextlib
import dataclasses
import glob
import hashlib
import json
import os
import re
import signal
import stat
import sys
import time

try:
    import fcntl
except ImportError:  # Pure decoding tests run on Windows.
    fcntl = None

try:
    import serial
except ImportError:  # Pure decoding tests do not need pyserial.
    serial = None


BUILD_ID = "20260717-incoming-sms-tts-r3"
CONTACT_NAME = os.environ.get("ELDER_SMS_CONTACT_NAME", "紧急联系人").strip()
CONTACT_NUMBER = os.environ.get("ELDER_SMS_PHONE", "").strip()
AT_PORT_GLOB = "/dev/serial/by-id/*ML307A*if02*"
RUNTIME_DIR = "/run/elder-assistant"
AT_LOCK_FILE = os.path.join(RUNTIME_DIR, "ml307a_at.lock")
STATE_DIR = "/home/space/.local/state/elder-incoming-sms"
STATE_FILE = os.path.join(STATE_DIR, "state.json")
REQUEST_FILE = os.path.join(RUNTIME_DIR, "elder_incoming_message_request.json")
PROCESSING_FILE = os.path.join(RUNTIME_DIR, "elder_incoming_message_processing.json")
RESULT_FILE = os.path.join(RUNTIME_DIR, "elder_incoming_message_result.json")
MAX_TEXT_CHARS = 50
MAX_STATE_ITEMS = 512
REQUEST_MAX_AGE = 600
CONTROL_TAG_RE = re.compile(r"\[(?:v|s|m|r|t)\d{1,3}\]", re.IGNORECASE)


class SmsDecodeError(ValueError):
    pass


class UnsupportedSms(SmsDecodeError):
    pass


@dataclasses.dataclass(frozen=True)
class SmsMessage:
    index: int
    status: int
    tpdu_length: int
    pdu_hex: str
    sender: str
    sender_normalized: str
    scts_hex: str
    dcs: int
    text: str
    fingerprint: str


def _decode_bcd_digits(data, digit_count):
    digits = []
    for value in data:
        digits.append(str(value & 0x0F))
        high = (value >> 4) & 0x0F
        if high != 0x0F:
            digits.append(str(high))
    result = "".join(digits[:digit_count])
    if not result.isdigit() or len(result) != digit_count:
        raise SmsDecodeError("invalid semi-octet address")
    return result


def normalize_phone(value):
    raw = str(value or "").strip()
    has_plus = raw.startswith("+")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("0086") and len(digits) == 15:
        digits = digits[4:]
    elif digits.startswith("86") and len(digits) == 13:
        digits = digits[2:]
    if len(digits) != 11 or not digits.startswith("1"):
        return None
    if has_plus and not raw.startswith("+86"):
        return None
    return digits


def mask_phone(value):
    normalized = normalize_phone(value)
    return "*******" + normalized[-4:] if normalized else "invalid"


def sanitize_text(value):
    text = str(value or "").replace("\x00", "")
    text = CONTROL_TAG_RE.sub("", text)
    text = "".join(" " if ch in "\r\n\t" else ch for ch in text)
    text = "".join(ch for ch in text if ord(ch) >= 0x20 and ord(ch) != 0x7F)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        raise SmsDecodeError("empty text after sanitization")
    if len(text) > MAX_TEXT_CHARS:
        raise UnsupportedSms("message exceeds %d characters" % MAX_TEXT_CHARS)
    try:
        text.encode("gb2312")
    except UnicodeEncodeError as exc:
        raise UnsupportedSms("message contains characters unsupported by TTS") from exc
    return text


def decode_sms_deliver_pdu(pdu_hex, index=0, status=0, tpdu_length=0):
    compact = re.sub(r"\s+", "", str(pdu_hex or "")).upper()
    if not compact or len(compact) % 2 or not re.fullmatch(r"[0-9A-F]+", compact):
        raise SmsDecodeError("invalid PDU hex")
    data = bytes.fromhex(compact)
    if len(data) < 2:
        raise SmsDecodeError("truncated PDU")

    smsc_length = data[0]
    cursor = 1 + smsc_length
    if cursor >= len(data):
        raise SmsDecodeError("invalid SMSC length")

    first_octet = data[cursor]
    cursor += 1
    if first_octet & 0x03 != 0:
        raise UnsupportedSms("PDU is not SMS-DELIVER")
    if first_octet & 0x40:
        raise UnsupportedSms("concatenated/UDH SMS is not supported")

    if cursor + 2 > len(data):
        raise SmsDecodeError("truncated originating address")
    address_digits = data[cursor]
    cursor += 1
    toa = data[cursor]
    cursor += 1
    if toa & 0x70 != 0x10:
        raise UnsupportedSms("alphanumeric sender is not supported")
    address_octets = (address_digits + 1) // 2
    if cursor + address_octets + 10 > len(data):
        raise SmsDecodeError("truncated SMS-DELIVER header")
    sender_digits = _decode_bcd_digits(
        data[cursor:cursor + address_octets], address_digits)
    cursor += address_octets
    sender = ("+" if toa & 0x90 == 0x90 else "") + sender_digits
    sender_normalized = normalize_phone(sender)

    cursor += 1  # TP-PID
    dcs = data[cursor]
    cursor += 1
    scts = data[cursor:cursor + 7]
    cursor += 7
    user_data_length = data[cursor]
    cursor += 1

    is_general_dcs = dcs & 0xC0 == 0
    is_ucs2 = is_general_dcs and dcs & 0x0C == 0x08 and not dcs & 0x20
    if not is_ucs2:
        raise UnsupportedSms("only single-part UCS2 SMS is supported (DCS=%02X)" % dcs)
    if user_data_length % 2:
        raise SmsDecodeError("odd UCS2 user-data length")
    if cursor + user_data_length > len(data):
        raise SmsDecodeError("truncated user data")
    raw_text = data[cursor:cursor + user_data_length]
    try:
        text = raw_text.decode("utf-16-be")
    except UnicodeDecodeError as exc:
        raise SmsDecodeError("invalid UCS2 body") from exc

    fingerprint = hashlib.sha256(
        ((sender_normalized or sender_digits) + "\0" + scts.hex().upper() + "\0" + compact)
        .encode("ascii")
    ).hexdigest()
    return SmsMessage(
        index=int(index),
        status=int(status),
        tpdu_length=int(tpdu_length or 0),
        pdu_hex=compact,
        sender=sender,
        sender_normalized=sender_normalized,
        scts_hex=scts.hex().upper(),
        dcs=dcs,
        text=text,
        fingerprint=fingerprint,
    )


CMGL_HEADER_RE = re.compile(r"^\+CMGL:\s*(\d+)\s*,\s*(\d+).*?,\s*(\d+)\s*$")
CMGR_HEADER_RE = re.compile(r"^\+CMGR:\s*(\d+).*?,\s*(\d+)\s*$")


def parse_cmgl_response(response):
    lines = [line.strip() for line in str(response).replace("\r", "").split("\n")]
    messages = []
    errors = []
    cursor = 0
    while cursor < len(lines):
        match = CMGL_HEADER_RE.match(lines[cursor])
        if not match:
            cursor += 1
            continue
        index, status, tpdu_length = map(int, match.groups())
        cursor += 1
        while cursor < len(lines) and not lines[cursor]:
            cursor += 1
        if cursor >= len(lines):
            errors.append((index, "missing PDU line"))
            break
        try:
            messages.append(decode_sms_deliver_pdu(
                lines[cursor], index=index, status=status, tpdu_length=tpdu_length))
        except SmsDecodeError as exc:
            errors.append((index, str(exc)))
        cursor += 1
    return messages, errors


def parse_cmgr_response(response, index):
    lines = [line.strip() for line in str(response).replace("\r", "").split("\n")]
    for pos, line in enumerate(lines):
        match = CMGR_HEADER_RE.match(line)
        if not match:
            continue
        status, tpdu_length = map(int, match.groups())
        for pdu_line in lines[pos + 1:]:
            if pdu_line:
                return decode_sms_deliver_pdu(
                    pdu_line, index=index, status=status, tpdu_length=tpdu_length)
    raise SmsDecodeError("CMGR response contains no message")


def _atomic_write_json(path, payload, mode=0o600):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    temp = "%s.%d.tmp" % (path, os.getpid())
    try:
        with open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        try:
            directory_fd = os.open(directory, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass
    finally:
        try:
            os.unlink(temp)
        except FileNotFoundError:
            pass


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _open_at_lock():
    flags = os.O_RDWR | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(AT_LOCK_FILE, flags)
    metadata = os.fstat(descriptor)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or
            metadata.st_mode & 0o007):
        os.close(descriptor)
        raise PermissionError("unsafe ML307A lock ownership or mode")
    return descriptor


@contextlib.contextmanager
def ml307a_at_lock(timeout=5.0):
    if fcntl is None:
        raise RuntimeError("fcntl is required on the board")
    descriptor = _open_at_lock()
    try:
        try:
            # Group access is required for the private cross-service IPC.
            os.fchmod(descriptor, 0o660)  # nosec B103
        except PermissionError:
            pass
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError("ML307A AT lock busy")
                time.sleep(0.10)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def find_at_port():
    if serial is None:
        raise RuntimeError("pyserial is required on the board")
    ports = sorted(glob.glob(AT_PORT_GLOB))
    if not ports:
        raise RuntimeError("ML307A if02 AT port is unavailable")
    return ports[0]


def _read_until_terminal(port, timeout):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = port.read(512)
        if chunk:
            data.extend(chunk)
            normalized = bytes(data).replace(b"\r", b"")
            lines = [line.strip() for line in normalized.split(b"\n") if line.strip()]
            if lines and (lines[-1] == b"OK" or lines[-1] == b"ERROR" or
                          lines[-1].startswith(b"+CME ERROR") or
                          lines[-1].startswith(b"+CMS ERROR")):
                break
    return bytes(data).decode("ascii", errors="replace")


def _response_ok(response):
    lines = [line.strip() for line in str(response).replace("\r", "").split("\n")]
    return "OK" in lines


def _response_summary(response):
    lines = [line.strip() for line in str(response).replace("\r", "").split("\n")
             if line.strip() and not line.strip().startswith("AT+")]
    return " | ".join(lines)[:240]


class ModemSession:
    def __init__(self):
        self.port = None
        self.original_cmgf = None

    def __enter__(self):
        path = find_at_port()
        self.port = serial.Serial(path, 115200, timeout=0.2, write_timeout=2)
        time.sleep(0.15)
        if not _response_ok(self.command("AT")):
            raise RuntimeError("ML307A did not answer AT")
        mode_response = self.command("AT+CMGF?")
        match = re.search(r"\+CMGF:\s*(\d+)", mode_response)
        self.original_cmgf = int(match.group(1)) if match else 1
        pdu_mode_response = self.command("AT+CMGF=0")
        if not _response_ok(pdu_mode_response):
            raise RuntimeError("ML307A rejected PDU receive mode: %s" %
                               _response_summary(pdu_mode_response))
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.port is not None:
            try:
                if self.original_cmgf is not None:
                    self.command("AT+CMGF=%d" % self.original_cmgf)
            except Exception as restore_error:
                print("[MODEM] WARN cannot restore CMGF: %s" % str(restore_error)[:100],
                      flush=True)
            self.port.close()

    def command(self, command, timeout=5.0):
        self.port.reset_input_buffer()
        self.port.write(command.encode("ascii") + b"\r\n")
        self.port.flush()
        response = _read_until_terminal(self.port, timeout)
        if not response:
            raise RuntimeError("no response for %s" % command)
        return response

    def storage_summary(self):
        return self.command("AT+CPMS?")

    def list_messages(self, status=4):
        response = self.command("AT+CMGL=%d" % int(status), timeout=10.0)
        messages, errors = parse_cmgl_response(response)
        return messages, errors

    def read_message(self, index):
        return parse_cmgr_response(self.command("AT+CMGR=%d" % index), index)

    def delete_message(self, index):
        response = self.command("AT+CMGD=%d" % index)
        return _response_ok(response)


def _default_state():
    return {
        "version": 1,
        "played": [],
        "rejected": [],
        "failures": {},
        "pending_delete": {},
        "backlog": [],
    }


def load_state():
    os.makedirs(STATE_DIR, mode=0o700, exist_ok=True)
    os.chmod(STATE_DIR, 0o700)
    try:
        state = _read_json(STATE_FILE)
    except FileNotFoundError:
        return _default_state()
    except Exception as exc:
        raise RuntimeError("cannot load dedup state: %s" % str(exc)) from exc
    if state.get("version") != 1:
        raise RuntimeError("unsupported dedup state version")
    for key, default in (("played", []), ("rejected", []), ("failures", {}),
                         ("pending_delete", {}), ("backlog", [])):
        if not isinstance(state.get(key), type(default)):
            state[key] = default
    return state


def save_state(state):
    state["played"] = state.get("played", [])[-MAX_STATE_ITEMS:]
    state["rejected"] = state.get("rejected", [])[-MAX_STATE_ITEMS:]
    failures = state.get("failures", {})
    if len(failures) > MAX_STATE_ITEMS:
        keep = sorted(failures, key=lambda key: failures[key].get("updated_at", 0))[-MAX_STATE_ITEMS:]
        state["failures"] = {key: failures[key] for key in keep}
    state["backlog"] = state.get("backlog", [])[-50:]
    _atomic_write_json(STATE_FILE, state, mode=0o600)


def _append_unique(items, value):
    if value in items:
        items.remove(value)
    items.append(value)


def _ipc_busy():
    return any(os.path.exists(path) for path in
               (REQUEST_FILE, PROCESSING_FILE, RESULT_FILE))


def submit_request(message):
    if _ipc_busy():
        return False
    request_id = "%d-%s" % (time.time_ns(), message.fingerprint[:12])
    payload = {
        "version": 1,
        "request_id": request_id,
        "sim_index": message.index,
        "message_fingerprint": message.fingerprint,
        "sender_normalized": message.sender_normalized,
        "sender_name": CONTACT_NAME,
        "received_at": message.scts_hex,
        "text": sanitize_text(message.text),
        "created_at": time.time(),
        "expires_at": time.time() + REQUEST_MAX_AGE,
    }
    _atomic_write_json(REQUEST_FILE, payload, mode=0o600)
    print("[IPC] queued id=%s index=%d chars=%d" %
          (request_id, message.index, len(payload["text"])), flush=True)
    return True


def handle_result(state):
    try:
        result = _read_json(RESULT_FILE)
    except FileNotFoundError:
        return False
    except Exception as exc:
        print("[IPC] invalid result retained: %s" % str(exc)[:100], flush=True)
        return False
    fingerprint = str(result.get("message_fingerprint", ""))
    status = str(result.get("status", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
        print("[IPC] invalid result fingerprint retained", flush=True)
        return False
    if status == "played":
        _append_unique(state["played"], fingerprint)
        state["failures"].pop(fingerprint, None)
        try:
            state["pending_delete"][fingerprint] = int(result.get("sim_index"))
        except (TypeError, ValueError):
            print("[IPC] played result has invalid SIM index; message retained", flush=True)
        save_state(state)
        print("[IPC] TTS completion persisted fingerprint=%s" % fingerprint[:12],
              flush=True)
    elif status in ("invalid", "expired"):
        _append_unique(state["rejected"], fingerprint)
        save_state(state)
        print("[IPC] request rejected status=%s fingerprint=%s" %
              (status, fingerprint[:12]), flush=True)
    elif status in ("failed", "interrupted"):
        previous = state["failures"].get(fingerprint, {})
        count = int(previous.get("count", 0)) + 1
        delay = min(300, 5 * (2 ** min(count - 1, 6)))
        state["failures"][fingerprint] = {
            "count": count,
            "next_retry": time.time() + delay,
            "updated_at": time.time(),
        }
        save_state(state)
        print("[IPC] TTS status=%s; retry in %ds fingerprint=%s" %
              (status, delay, fingerprint[:12]), flush=True)
    else:
        print("[IPC] unknown result status retained: %s" % status, flush=True)
        return False
    try:
        os.unlink(RESULT_FILE)
    except FileNotFoundError:
        pass
    return True


def _retry_ready(state, fingerprint):
    failure = state.get("failures", {}).get(fingerprint)
    return not failure or time.time() >= float(failure.get("next_retry", 0))


def _message_to_state(message):
    return {
        "index": message.index,
        "sender_normalized": message.sender_normalized,
        "scts_hex": message.scts_hex,
        "dcs": message.dcs,
        "text": message.text,
        "fingerprint": message.fingerprint,
    }


def _message_from_state(payload):
    return SmsMessage(
        index=int(payload["index"]), status=1, tpdu_length=0, pdu_hex="",
        sender=payload["sender_normalized"],
        sender_normalized=payload["sender_normalized"],
        scts_hex=payload["scts_hex"], dcs=int(payload["dcs"]),
        text=payload["text"], fingerprint=payload["fingerprint"])


def _prune_backlog(state):
    terminal = set(state.get("played", ())) | set(state.get("rejected", ()))
    previous = state.get("backlog", [])
    state["backlog"] = [
        item for item in state.get("backlog", [])
        if item.get("fingerprint") not in terminal
    ]
    return len(previous) != len(state["backlog"])


def _submit_backlog_if_ready(state):
    if _ipc_busy():
        return False
    for payload in state.get("backlog", []):
        try:
            message = _message_from_state(payload)
        except (TypeError, ValueError):
            continue
        if _retry_ready(state, message.fingerprint):
            return submit_request(message)
    return False


def import_message_index(state, index):
    """Import one retained/read message for a controlled end-to-end test."""
    if _ipc_busy():
        raise RuntimeError("incoming-message IPC is busy")
    with ml307a_at_lock(timeout=3.0):
        with ModemSession() as modem:
            message = modem.read_message(int(index))
    if message.sender_normalized != CONTACT_NUMBER:
        raise RuntimeError("message index %d is not from the trusted contact" % index)
    clean_text = sanitize_text(message.text)
    if clean_text != message.text:
        message = dataclasses.replace(message, text=clean_text)
    known = {item.get("fingerprint") for item in state.get("backlog", [])}
    if (message.fingerprint not in known and
            message.fingerprint not in state.get("played", []) and
            message.fingerprint not in state.get("rejected", [])):
        state["backlog"].append(_message_to_state(message))
        save_state(state)
    print("[IMPORT] retained index=%d sender=%s chars=%d fingerprint=%s" %
          (message.index, mask_phone(message.sender), len(message.text),
           message.fingerprint[:12]), flush=True)
    return message


def service_poll(state, allow_delete=True):
    handle_result(state)
    if _prune_backlog(state):
        save_state(state)
    if _submit_backlog_if_ready(state):
        return
    if _ipc_busy():
        return
    with ml307a_at_lock():
        with ModemSession() as modem:
            pending_delete = dict(state.get("pending_delete", {}))
            for fingerprint, index in pending_delete.items():
                if not allow_delete:
                    continue
                try:
                    current = modem.read_message(int(index))
                except SmsDecodeError as exc:
                    print("[SMS] confirmed message retained index=%s reason=%s" %
                          (index, str(exc)[:100]), flush=True)
                    continue
                if current.fingerprint != fingerprint:
                    print("[SMS] delete refused: index changed (%s)" % index,
                          flush=True)
                    state["pending_delete"].pop(fingerprint, None)
                    save_state(state)
                    continue
                if modem.delete_message(int(index)):
                    print("[SMS] deleted after played confirmation index=%s fingerprint=%s" %
                          (index, fingerprint[:12]), flush=True)
                    state["pending_delete"].pop(fingerprint, None)
                    save_state(state)

            # Query only unread records. Every decoded record is persisted to a
            # private backlog before this poll returns, so multiple arrivals are
            # not lost when CMGL changes their status to read.
            messages, errors = modem.list_messages(status=0)
            for index, error in errors:
                print("[SMS] ignored index=%d reason=%s" % (index, error[:100]), flush=True)

            for message in messages:
                if message.fingerprint not in state["played"]:
                    continue
                if not allow_delete:
                    continue
                current = modem.read_message(message.index)
                if current.fingerprint != message.fingerprint:
                    print("[SMS] delete refused: index changed (%d)" % message.index,
                          flush=True)
                    continue
                if modem.delete_message(message.index):
                    print("[SMS] deleted after played confirmation index=%d fingerprint=%s" %
                          (message.index, message.fingerprint[:12]), flush=True)

            backlog_fingerprints = {
                item.get("fingerprint") for item in state.get("backlog", [])
            }
            backlog_changed = False
            for message in messages:
                if message.fingerprint in state["played"] or \
                        message.fingerprint in state["rejected"] or \
                        message.fingerprint in backlog_fingerprints:
                    continue
                if message.sender_normalized != CONTACT_NUMBER:
                    _append_unique(state["rejected"], message.fingerprint)
                    save_state(state)
                    print("[SMS] rejected non-whitelist sender=%s index=%d" %
                          (mask_phone(message.sender), message.index), flush=True)
                    continue
                if not _retry_ready(state, message.fingerprint):
                    continue
                try:
                    clean_text = sanitize_text(message.text)
                except SmsDecodeError as exc:
                    _append_unique(state["rejected"], message.fingerprint)
                    save_state(state)
                    print("[SMS] rejected index=%d reason=%s" %
                          (message.index, str(exc)[:100]), flush=True)
                    continue
                if clean_text != message.text:
                    message = dataclasses.replace(message, text=clean_text)
                state["backlog"].append(_message_to_state(message))
                backlog_fingerprints.add(message.fingerprint)
                backlog_changed = True
            if backlog_changed:
                save_state(state)
    _submit_backlog_if_ready(state)


def read_only_scan(seen=None):
    seen = seen if seen is not None else set()
    with ml307a_at_lock(timeout=3.0):
        with ModemSession() as modem:
            storage = " ".join(line.strip() for line in modem.storage_summary().splitlines()
                               if line.strip() and not line.strip().startswith("AT+"))
            # The hardware gate only inspects unread messages. This avoids the
            # large legacy ME archive and still preserves every message body.
            messages, errors = modem.list_messages(status=0)
    print("[READONLY] storage=%s messages=%d errors=%d" %
          (storage[:160], len(messages), len(errors)), flush=True)
    for index, error in errors:
        error_key = "error:%d:%s" % (index, error)
        if error_key in seen:
            continue
        seen.add(error_key)
        print("[READONLY] index=%d ignored=%s" % (index, error[:120]), flush=True)
    for message in messages:
        if message.fingerprint in seen:
            continue
        seen.add(message.fingerprint)
        trusted = message.sender_normalized == CONTACT_NUMBER
        try:
            clean = sanitize_text(message.text)
            clean_status = "accepted"
        except SmsDecodeError as exc:
            clean = "<not printable>"
            clean_status = "rejected:%s" % str(exc)
        print("[READONLY] index=%d sender=%s trusted=%s dcs=%02X chars=%d "
              "status=%s fingerprint=%s text=%s" %
              (message.index, mask_phone(message.sender), trusted, message.dcs,
               len(message.text), clean_status, message.fingerprint[:12], clean), flush=True)
    return seen


def parse_args(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--read-only", action="store_true",
                        help="decode only; never create IPC or delete messages")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--watch-seconds", type=float, default=0)
    parser.add_argument("--poll-interval", type=float, default=5.0)
    parser.add_argument("--no-delete", action="store_true",
                        help="service IPC is enabled but confirmed messages are retained")
    parser.add_argument("--import-index", type=int,
                        help="import one retained message before normal processing")
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.watch_seconds and not args.read_only:
        raise SystemExit("--watch-seconds requires --read-only")
    if args.read_only and args.import_index is not None:
        raise SystemExit("--import-index cannot be combined with --read-only")
    print("[INIT] incoming SMS receiver build=%s mode=%s" %
          (BUILD_ID, "read-only" if args.read_only else "service"), flush=True)
    running = [True]

    def stop(_signum, _frame):
        running[0] = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    if args.read_only:
        deadline = time.monotonic() + args.watch_seconds if args.watch_seconds else None
        seen = set()
        while running[0]:
            try:
                seen = read_only_scan(seen)
            except TimeoutError as exc:
                print("[READONLY] deferred: %s" % exc, flush=True)
            except Exception as exc:
                print("[READONLY] error: %s" % str(exc)[:160], flush=True)
            if args.once or deadline is None or time.monotonic() >= deadline:
                break
            time.sleep(max(1.0, args.poll_interval))
        return 0

    state = load_state()
    if args.import_index is not None:
        import_message_index(state, args.import_index)
        if not _submit_backlog_if_ready(state):
            raise SystemExit("imported message could not be submitted")
        return 0
    backoff = 2.0
    while running[0]:
        try:
            service_poll(state, allow_delete=not args.no_delete)
            backoff = 2.0
            if args.once:
                break
            time.sleep(max(1.0, args.poll_interval))
        except TimeoutError as exc:
            print("[MODEM] poll deferred: %s" % exc, flush=True)
            time.sleep(max(1.0, args.poll_interval))
        except Exception as exc:
            print("[ERROR] poll failed; retry in %.0fs: %s" %
                  (backoff, str(exc)[:160]), flush=True)
            if args.once:
                return 1
            time.sleep(backoff)
            backoff = min(30.0, backoff * 2)
    return 0


if __name__ == "__main__":
    sys.exit(main())
