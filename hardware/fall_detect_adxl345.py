#!/usr/bin/env python3
"""K1 摔倒检测 + 持续红灯闪烁 + UCS2中文SMS告警（ADXL345版）。

判定链：短窗口累计失重 -> 冲击。当前按调试要求不做撞击后静止确认。
使用 --no-sms 可保留灯带和检测日志，但禁止发送真实短信。
摔倒警报触发后持续闪烁，直到语音助手提交“关闭警报”确认请求。
"""
import os, fcntl, struct, time, math, signal, serial, subprocess, glob, threading, sys, json, re, stat, tempfile
from contextlib import contextmanager

# ============ 配置 ============
I2C_SLAVE    = 0x0703
ADXL_ADDR    = 0x53
I2C_DEV      = '/dev/i2c-3'
WS2812_BIN   = '/usr/local/libexec/elder-assistant/ws2812'
SMS_PHONE    = os.environ.get('ELDER_SMS_PHONE', '').strip()
SMS_TEXT     = '老人摔倒！请到软件端查看老人实时位置！'
SMS_RESOLVED_TEXT = '老人已得到帮助！'
SMS_MAX_ATTEMPTS = 5
USER_SMS_MAX_ATTEMPTS = 2
AT_PROBE_LOCK_TIMEOUT = 2
RUNTIME_DIR = '/run/elder-assistant'
ML307A_AT_LOCK = os.path.join(RUNTIME_DIR, 'ml307a_at.lock')

BUILD_ID = '20260717-voice-user-sms-fall-window-r7'
ALERT_ACTIVE_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_active')
ALERT_ACK_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_ack_request')
INITIAL_SMS_DONE_FILE = os.path.join(RUNTIME_DIR, 'fall_initial_sms_done')
ALERT_RESOLVING_FILE = os.path.join(RUNTIME_DIR, 'fall_alert_resolving')
YELLOW_FEEDBACK_ACTIVE_FILE = os.path.join(RUNTIME_DIR, 'radar_listening_yellow_active')
USER_SMS_REQUEST_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_request.json')
USER_SMS_PROCESSING_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_processing.json')
USER_SMS_RESULT_FILE = os.path.join(RUNTIME_DIR, 'elder_user_sms_result.json')

FREE_FALL_THRESH     = 0.65  # 老人摔倒友好：不过度收紧失重阈值
FREE_FALL_WINDOW_SEC = 0.12  # 在短滑动窗口内累计低加速度
FREE_FALL_ACCUM_SEC  = 0.06  # 累计达到 60ms；过滤实测仅 55~56ms 的普通移动
IMPACT_THRESH     = 1.80  # 适度提高，过滤 1.5~1.7g 的普通急停
FALL_WINDOW_SEC   = 0.80  # ARM 后等待冲击；缩短以过滤随后人为抬升
IMPACT_JERK_MIN   = 40.0  # g/s，过滤实测 20~35g/s 的普通抬升
IMPACT_VERIFY_SEC = 0.25  # 峰值后观察短脉冲形状，不是静止确认
IMPACT_RELEASE_G  = 1.30  # 250ms 内回落至该值以下才算短促撞击
COOLDOWN_SEC      = 30
SAMPLE_INTERVAL   = 0.01  # 100Hz，减少漏掉短暂失重/冲击峰值

CRLF = '\r\n'
SMS_ENABLED = '--no-sms' not in sys.argv
ACTIVE_COOLDOWN_SEC = COOLDOWN_SEC if SMS_ENABLED else 0

# ============ ML307A ============
_at_process_lock = threading.RLock()

def _open_ml307a_at_lock():
    flags = os.O_RDWR | getattr(os, 'O_CLOEXEC', 0) | getattr(os, 'O_NOFOLLOW', 0)
    lock_fd = os.open(ML307A_AT_LOCK, flags)
    metadata = os.fstat(lock_fd)
    if (not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != 0 or
            metadata.st_mode & 0o007):
        os.close(lock_fd)
        raise PermissionError('unsafe ML307A lock ownership or mode')
    return lock_fd

@contextmanager
def ml307a_at_lock(timeout=None):
    """与定位服务协作独占AT串口，但不停止OneNET MQTT服务。"""
    with _at_process_lock:
        lock_fd = _open_ml307a_at_lock()
        try:
            try:
                # Group access is required for the private cross-service IPC.
                os.fchmod(lock_fd, 0o660)  # nosec B103
            except PermissionError:
                pass
            if timeout is None:
                # 真正短信发送必须排队拿到串口，不因定位扫描超时丢弃。
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
            else:
                deadline = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= deadline:
                            raise TimeoutError('timed out waiting for ML307A AT lock')
                        time.sleep(0.10)
            yield
        finally:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(lock_fd)

def _find_at_port_unlocked():
    stable = sorted(glob.glob('/dev/serial/by-id/*ML307A*if02*'))
    # 只探测ML307A的AT接口。回退扫描所有ttyUSB会误开TTS串口，且可能
    # 在ML307A重枚举期间触碰其他接口并延长恢复时间。
    for dev in stable:
        try:
            s = serial.Serial(dev, 115200, timeout=0.5, write_timeout=1)
            s.write(b'AT\r\n'); time.sleep(0.3)
            r = s.read(200).decode(errors='replace')
            s.close()
            if 'OK' in r: return dev
        except: pass
    return None

def find_at_port():
    try:
        with ml307a_at_lock(timeout=AT_PROBE_LOCK_TIMEOUT):
            return _find_at_port_unlocked()
    except TimeoutError as e:
        print('[SMS] AT port probe deferred: %s' % str(e), flush=True)
        return None

# ============ SMS (UCS2 PDU) ============
def _bcd_encode(number):
    digits = [int(char) for char in number]
    encoded = []
    for index in range(0, len(digits), 2):
        low = digits[index]
        high = digits[index + 1] if index + 1 < len(digits) else 0xF
        encoded.append((high << 4) | low)
    return bytes(encoded)

def _build_ucs2_pdu(phone, text):
    international_phone = '86' + phone
    destination = _bcd_encode(international_phone)
    user_data = text.encode('utf-16-be')
    if len(user_data) > 140:
        raise ValueError('UCS2 SMS exceeds one-message payload')
    tpdu = (
        b'\x11\x00' +
        bytes([len(international_phone)]) +
        b'\x91' + destination +
        b'\x00\x08\x01' +
        bytes([len(user_data)]) + user_data
    )
    return b'\x00' + tpdu, len(tpdu)

def _read_until(serial_port, timeout, tokens):
    deadline = time.monotonic() + timeout
    data = bytearray()
    while time.monotonic() < deadline:
        chunk = serial_port.read(256)
        if chunk:
            data.extend(chunk)
            if any(token in data for token in tokens):
                break
    return bytes(data)

def _send_at(serial_port, command, timeout=3.0):
    serial_port.reset_input_buffer()
    serial_port.write(command.encode('ascii') + b'\r\n')
    serial_port.flush()
    return _read_until(
        serial_port, timeout, (b'OK', b'ERROR', b'+CME ERROR', b'+CMS ERROR'))

def _send_pdu_once(at_port, phone, text):
    pdu, tpdu_length = _build_ucs2_pdu(phone, text)
    try:
        modem = serial.Serial(at_port, 115200, timeout=0.2, write_timeout=3)
    except Exception as e:
        print('[SMS] AT port open failed (%s): %s' %
              (at_port, str(e)[:120]), flush=True)
        return False, True
    retryable = True
    try:
        time.sleep(0.2)
        for command in ('AT', 'AT+CMEE=2', 'AT+CPIN?', 'AT+CEREG?'):
            response = _send_at(modem, command)
            if b'OK' not in response:
                print('[SMS] %s failed: %s' %
                      (command, response.decode('ascii', errors='replace').strip()[:120]),
                      flush=True)
                return False, True

        response = _send_at(modem, 'AT+CMGF=0')
        if b'OK' not in response:
            print('[SMS] PDU mode rejected: %s' %
                  response.decode('ascii', errors='replace').strip()[:120], flush=True)
            return False, True

        modem.reset_input_buffer()
        modem.write(('AT+CMGS=%d\r\n' % tpdu_length).encode('ascii'))
        modem.flush()
        prompt = _read_until(modem, 5.0, (b'>', b'ERROR', b'+CMS ERROR'))
        if b'>' not in prompt:
            modem.write(b'\x1b')
            print('[SMS] No CMGS prompt: %s' %
                  prompt.decode('ascii', errors='replace').strip()[:120], flush=True)
            return False, True

        modem.write(pdu.hex().upper().encode('ascii') + b'\x1a')
        modem.flush()
        final = _read_until(modem, 30.0, (b'OK', b'ERROR', b'+CMS ERROR'))
        final_text = final.decode('ascii', errors='replace').strip()
        print('[SMS] PDU result: %s' % final_text[:200], flush=True)
        success = b'+CMGS:' in final and b'OK' in final
        if success:
            return True, False

        # 回执丢失时由上层决定是否进行有限重试。
        return False, True
    finally:
        try:
            response = _send_at(modem, 'AT+CMGF=1')
            print('[SMS] Restore TEXT mode: %s' %
                  response.decode('ascii', errors='replace').strip()[:80], flush=True)
        except Exception as e:
            print('[SMS] WARN cannot restore TEXT mode: %s' % str(e)[:80], flush=True)
        modem.close()

def send_sms(at_port, text, phone=SMS_PHONE, max_attempts=SMS_MAX_ATTEMPTS,
             abort_if_fall=False):
    try:
        with ml307a_at_lock():
            print('[SMS] AT lock acquired; OneNET remains online', flush=True)
            for attempt in range(1, max_attempts + 1):
                if abort_if_fall and not _user_sms_allowed():
                    print('[USER-SMS] Aborted before AT transaction: fall SMS transition active',
                          flush=True)
                    return False
                print('[SMS] UCS2 PDU attempt %d/%d' %
                      (attempt, max_attempts), flush=True)
                current_port = _find_at_port_unlocked()
                if not current_port:
                    print('[SMS] No responsive AT port on attempt %d' % attempt,
                          flush=True)
                    success, retryable = False, True
                else:
                    if current_port != at_port:
                        print('[SMS] AT port refreshed: %s -> %s' %
                              (at_port, current_port), flush=True)
                    at_port = current_port
                    success, retryable = _send_pdu_once(at_port, phone, text)
                if success:
                    return True
                if abort_if_fall and not _user_sms_allowed():
                    print('[USER-SMS] Aborted retry: fall SMS transition active', flush=True)
                    return False
                if not retryable or attempt == max_attempts:
                    return False
                time.sleep(1.0)
            return False

    except Exception as e:
        print('[SMS] Error: %s' % str(e)[:100], flush=True)
        return False

# ============ 老人语音普通短信 IPC ============
def _atomic_write_json(path, payload):
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='utf-8', dir=RUNTIME_DIR,
                prefix='.fall-ipc-', delete=False) as f:
            tmp = f.name
            json.dump(payload, f, ensure_ascii=False, separators=(',', ':'))
            f.write('\n')
            f.flush()
            os.fsync(f.fileno())
            # Group access is required for the private cross-service IPC.
            os.fchmod(f.fileno(), 0o660)  # nosec
        os.replace(tmp, path)
    finally:
        if tmp and os.path.exists(tmp):
            try: os.unlink(tmp)
            except OSError: pass

def _write_user_sms_result(request_id, status):
    _atomic_write_json(USER_SMS_RESULT_FILE, {
        'version': 1,
        'request_id': request_id,
        'status': status,
        'completed_unix': time.time(),
    })

def _user_sms_allowed():
    """允许空闲期，或摔倒首条短信结束后到解除警报前的窗口。"""
    if os.path.exists(ALERT_RESOLVING_FILE):
        return False
    return (not os.path.exists(ALERT_ACTIVE_FILE) or
            os.path.exists(INITIAL_SMS_DONE_FILE))

def _validate_user_sms_request(request):
    request_id = str(request.get('request_id', '')).strip()
    phone = re.sub(r'\D', '', str(request.get('phone', '')))
    text = str(request.get('text', '')).strip()
    expires_unix = float(request.get('expires_unix', 0) or 0)
    if not request_id:
        return None, 'invalid'
    if phone != SMS_PHONE:
        return request_id, 'invalid'
    try:
        user_data = text.encode('utf-16-be')
    except UnicodeEncodeError:
        return request_id, 'invalid'
    if not text or len(user_data) > 140:
        return request_id, 'invalid'
    if expires_unix <= time.time():
        return request_id, 'expired'
    return (request_id, phone, text), None

def _recover_interrupted_user_sms():
    try:
        with open(USER_SMS_PROCESSING_FILE, 'r', encoding='utf-8') as f:
            request = json.load(f)
        request_id = str(request.get('request_id', '')).strip()
        if request_id:
            _write_user_sms_result(request_id, 'interrupted')
            print('[USER-SMS] Previous in-flight request marked interrupted id=%s' %
                  request_id, flush=True)
    except FileNotFoundError:
        return
    except Exception as e:
        print('[USER-SMS] Cannot recover processing file: %s' % str(e)[:100],
              flush=True)
    finally:
        try: os.unlink(USER_SMS_PROCESSING_FILE)
        except FileNotFoundError: pass
        except OSError as e:
            print('[USER-SMS] Cannot clear processing file: %s' % str(e)[:100],
                  flush=True)

def _user_sms_worker(run):
    print('[USER-SMS] Request worker ready', flush=True)
    while run[0]:
        if not _user_sms_allowed():
            time.sleep(0.10)
            continue
        try:
            os.replace(USER_SMS_REQUEST_FILE, USER_SMS_PROCESSING_FILE)
        except FileNotFoundError:
            time.sleep(0.10)
            continue
        except OSError as e:
            print('[USER-SMS] Cannot claim request: %s' % str(e)[:100], flush=True)
            time.sleep(0.20)
            continue

        request_id = ''
        try:
            with open(USER_SMS_PROCESSING_FILE, 'r', encoding='utf-8') as f:
                request = json.load(f)
            validated, error = _validate_user_sms_request(request)
            if error:
                request_id = validated or str(request.get('request_id', '')).strip()
                if request_id:
                    _write_user_sms_result(request_id, error)
                print('[USER-SMS] Rejected request id=%s status=%s' %
                      (request_id or '?', error), flush=True)
                continue

            request_id, phone, text = validated
            if not _user_sms_allowed():
                _write_user_sms_result(request_id, 'blocked_by_fall')
                print('[USER-SMS] Blocked by fall SMS transition id=%s' % request_id,
                      flush=True)
                continue

            print('[USER-SMS] Sending id=%s phone=*******%s chars=%d' %
                  (request_id, phone[-4:], len(text)), flush=True)
            ok = send_sms(
                None, text, phone=phone,
                max_attempts=USER_SMS_MAX_ATTEMPTS, abort_if_fall=True)
            if ok:
                status = 'sent'
            elif not _user_sms_allowed():
                status = 'blocked_by_fall'
            else:
                status = 'failed'
            _write_user_sms_result(request_id, status)
            print('[USER-SMS] Result id=%s status=%s' %
                  (request_id, status), flush=True)
        except Exception as e:
            print('[USER-SMS] Worker error id=%s: %s' %
                  (request_id or '?', str(e)[:120]), flush=True)
            if request_id:
                try: _write_user_sms_result(request_id, 'failed')
                except OSError: pass
        finally:
            try: os.unlink(USER_SMS_PROCESSING_FILE)
            except FileNotFoundError: pass
            except OSError as e:
                print('[USER-SMS] Cannot clear processing file: %s' %
                      str(e)[:100], flush=True)

# ============ 摔倒警报 IPC（与语音助手解耦） ============
def _remove_marker(path):
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError as e:
        print('[ALARM] WARN cannot remove %s: %s' % (path, str(e)[:80]), flush=True)

def _write_active_marker(alert_id):
    tmp = None
    content = (
        'version=1\n'
        'alert_id=%s\n'
        'pid=%d\n'
        'started_unix=%.3f\n'
        'sms_enabled=%d\n'
    ) % (alert_id, os.getpid(), time.time(), 1 if SMS_ENABLED else 0)
    try:
        with tempfile.NamedTemporaryFile(
                mode='w', encoding='ascii', dir=RUNTIME_DIR,
                prefix='.fall-active-', delete=False) as f:
            tmp = f.name
            f.write(content)
            f.flush()
            os.fsync(f.fileno())
            # Group access is required for the private cross-service IPC.
            os.fchmod(f.fileno(), 0o660)  # nosec
        os.replace(tmp, ALERT_ACTIVE_FILE)
    finally:
        if tmp and os.path.exists(tmp):
            _remove_marker(tmp)

def _write_phase_marker(path, alert_id, phase):
    _atomic_write_json(path, {
        'version': 1,
        'alert_id': alert_id,
        'phase': phase,
        'updated_unix': time.time(),
    })

def _publish_initial_sms_done(alert_id):
    """仅为仍活动的同一次警报开放普通短信窗口。"""
    if os.path.exists(ALERT_RESOLVING_FILE):
        return
    try:
        with open(ALERT_ACTIVE_FILE, 'r', encoding='ascii', errors='replace') as f:
            active = dict(line.split('=', 1) for line in f.read().splitlines()
                          if '=' in line)
    except (FileNotFoundError, OSError, ValueError):
        return
    if active.get('alert_id') != alert_id:
        return
    _write_phase_marker(INITIAL_SMS_DONE_FILE, alert_id, 'initial_sms_done')
    print('[ALARM] Initial SMS transaction complete; user SMS window OPEN id=%s' %
          alert_id, flush=True)

def activate_fall_alarm():
    """发布当前警报 ID，并清掉上一次遗留的确认请求。"""
    _remove_marker(ALERT_ACK_FILE)
    _remove_marker(INITIAL_SMS_DONE_FILE)
    _remove_marker(ALERT_RESOLVING_FILE)
    alert_id = '%d-%d' % (int(time.time() * 1000), os.getpid())
    _write_active_marker(alert_id)
    print('[ALARM] Active id=%s; waiting for voice acknowledgement' % alert_id, flush=True)
    return alert_id

def clear_fall_alarm_ipc():
    _remove_marker(ALERT_ACTIVE_FILE)
    _remove_marker(ALERT_ACK_FILE)
    _remove_marker(INITIAL_SMS_DONE_FILE)

def consume_fall_alarm_ack(alert_id):
    """只接受与当前警报 ID 一致的请求，避免旧请求关闭下一次警报。"""
    try:
        with open(ALERT_ACK_FILE, 'r', encoding='ascii', errors='replace') as f:
            request_id = f.read(256).strip()
    except FileNotFoundError:
        return False
    except OSError as e:
        print('[ALARM] WARN cannot read acknowledgement: %s' % str(e)[:80], flush=True)
        return False

    _remove_marker(ALERT_ACK_FILE)
    if request_id == alert_id:
        print('[ALARM] Voice acknowledgement accepted id=%s' % alert_id, flush=True)
        return True
    print('[ALARM] Ignored stale acknowledgement id=%s' % request_id[:80], flush=True)
    return False

# ============ WS2812B ============
_flash_running = False

def _sudo(cmd_list):
    if os.geteuid() != 0:
        raise PermissionError('fall detection must run as root')
    subprocess.run(cmd_list, stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL)

def _yellow_feedback_active():
    return os.path.exists(YELLOW_FEEDBACK_ACTIVE_FILE)

def _wait_red_flash_phase(seconds):
    """等待一个红闪相位；黄色反馈开始时立即让权。"""
    deadline = time.monotonic() + seconds
    while _flash_running and time.monotonic() < deadline:
        if _yellow_feedback_active():
            return False
        time.sleep(min(0.03, deadline - time.monotonic()))
    return not _yellow_feedback_active()

def start_red_flash():
    global _flash_running
    _flash_running = True
    def _flash():
        yellow_yielding = False
        while _flash_running:
            if _yellow_feedback_active():
                if not yellow_yielding:
                    print('[ACTION] Red flash yielding to listening yellow feedback', flush=True)
                yellow_yielding = True
                time.sleep(0.03)
                continue
            if yellow_yielding:
                print('[ACTION] Listening yellow feedback complete; red flash resumed', flush=True)
                yellow_yielding = False
            _sudo([WS2812_BIN, 'red'])
            if not _wait_red_flash_phase(0.15):
                continue
            if _yellow_feedback_active():
                continue
            _sudo([WS2812_BIN, 'off'])
            _wait_red_flash_phase(0.15)
    threading.Thread(target=_flash, daemon=True).start()

def stop_red_flash():
    global _flash_running
    _flash_running = False
    deadline = time.monotonic() + 2.0
    while _yellow_feedback_active() and time.monotonic() < deadline:
        time.sleep(0.03)
    time.sleep(0.3)
    _sudo([WS2812_BIN, 'off'])

def handle_active_fall_alarm(at_port, run):
    """执行一次警报生命周期；返回可复用的AT端口。

    灯带在此函数返回前始终由摔倒程序持有。语音助手只写确认请求；第一条和
    解除短信均在本函数内串行发送，保证不会争抢ML307A串口。
    """
    alert_id = activate_fall_alarm()
    start_red_flash()
    print('[ACTION] Red flash ON (persistent until voice acknowledgement)', flush=True)

    if not at_port:
        at_port = find_at_port()
        if at_port:
            print('[RECOVER] AT port: %s' % at_port, flush=True)

    initial_sms_thread = None
    if not SMS_ENABLED:
        print('[SMS] Disabled by --no-sms, initial SMS skipped', flush=True)
        _publish_initial_sms_done(alert_id)
    else:
        def _send_initial_sms(port):
            try:
                print('[SMS] Sending initial: %s' % SMS_TEXT, flush=True)
                ok = send_sms(port, SMS_TEXT)
                print('[SMS] Initial result: %s' % ('OK' if ok else 'FAIL'), flush=True)
            finally:
                _publish_initial_sms_done(alert_id)
        initial_sms_thread = threading.Thread(
            target=_send_initial_sms, args=(at_port,),
            name='fall-initial-sms', daemon=False)
        initial_sms_thread.start()

    acknowledged = False
    print('[ALARM] Red flash will continue until “关闭警报” is confirmed', flush=True)
    try:
        while run[0]:
            if consume_fall_alarm_ack(alert_id):
                acknowledged = True
                _write_phase_marker(ALERT_RESOLVING_FILE, alert_id, 'resolving')
                _remove_marker(INITIAL_SMS_DONE_FILE)
                print('[ALARM] User SMS window CLOSED; resolving id=%s' % alert_id,
                      flush=True)
                break
            time.sleep(0.10)
    finally:
        stop_red_flash()
        clear_fall_alarm_ipc()
        print('[ACTION] Red flash OFF', flush=True)

        # 第一条短信可能仍在发送；必须串行等待，避免两个线程抢同一AT串口。
        if initial_sms_thread is not None:
            initial_sms_thread.join()

    try:
        if acknowledged and SMS_ENABLED:
            print('[SMS] Sending resolved: %s' % SMS_RESOLVED_TEXT, flush=True)
            ok = send_sms(at_port, SMS_RESOLVED_TEXT)
            print('[SMS] Resolved result: %s' % ('OK' if ok else 'FAIL'), flush=True)
            at_port = find_at_port() or at_port
        elif acknowledged:
            print('[SMS] Disabled by --no-sms, resolved SMS skipped', flush=True)
        else:
            print('[ALARM] Stopped by process shutdown; resolved SMS not sent', flush=True)
    finally:
        _remove_marker(ALERT_RESOLVING_FILE)
    return at_port

# ============ 摔倒检测状态机 ============
class FallDetector:
    """120ms 内累计低加速度 60ms -> 0.8s 内短促冲击。无静止/姿态判定。"""

    def __init__(self):
        self.reset()

    def reset(self):
        self.state = 'NORMAL'
        self.low_history = []
        self.freefall_start = None
        self.freefall_armed = None
        self.freefall_min = None
        self.freefall_low_sec = 0.0
        self.prev_time = None
        self.prev_a = None
        self.wait_max_a = 0.0
        self.wait_max_jerk = 0.0
        self.impact_time = None
        self.impact_peak = None
        self.impact_max_jerk = None

    @staticmethod
    def _low_duration(samples):
        """按相邻采样时间积分低加速度时长，不要求低值连续。"""
        total = 0.0
        for i in range(1, len(samples)):
            t0, a0 = samples[i - 1]
            t1, _ = samples[i]
            if a0 < FREE_FALL_THRESH:
                total += min(max(t1 - t0, 0.0), 0.03)
        return total

    def update(self, t_now, a_total):
        if self.prev_time is None:
            jerk = 0.0
        else:
            dt = max(t_now - self.prev_time, 1e-4)
            jerk = abs(a_total - self.prev_a) / dt
        self.prev_time = t_now
        self.prev_a = a_total

        if self.state == 'NORMAL':
            self.low_history.append((t_now, a_total))
            cutoff = t_now - FREE_FALL_WINDOW_SEC
            self.low_history = [(t, a) for t, a in self.low_history if t >= cutoff]
            low_samples = [(t, a) for t, a in self.low_history if a < FREE_FALL_THRESH]
            low_sec = self._low_duration(self.low_history)
            if low_samples and low_sec >= FREE_FALL_ACCUM_SEC:
                self.freefall_start = low_samples[0][0]
                self.freefall_armed = t_now
                self.freefall_min = min(a for _, a in low_samples)
                self.freefall_low_sec = low_sec
                self.wait_max_a = a_total
                self.wait_max_jerk = jerk
                self.state = 'WAIT_IMPACT'
                return {
                    'kind': 'armed',
                    'freefall_min': self.freefall_min,
                    'low_ms': self.freefall_low_sec * 1000.0,
                    'window_ms': FREE_FALL_WINDOW_SEC * 1000.0,
                }
            return None

        if self.state == 'WAIT_IMPACT':
            self.freefall_min = min(self.freefall_min, a_total)
            self.wait_max_a = max(self.wait_max_a, a_total)
            self.wait_max_jerk = max(self.wait_max_jerk, jerk)
            if a_total >= IMPACT_THRESH and jerk >= IMPACT_JERK_MIN:
                self.impact_time = t_now
                self.impact_peak = a_total
                self.impact_max_jerk = jerk
                self.state = 'VERIFY_IMPACT'
                event = {
                    'kind': 'impact_candidate',
                    'freefall_min': self.freefall_min,
                    'low_ms': self.freefall_low_sec * 1000.0,
                    'impact_peak': a_total,
                    'impact_jerk': jerk,
                    'impact_delay_ms': (t_now - self.freefall_armed) * 1000.0,
                }
                return event
            if t_now - self.freefall_armed > FALL_WINDOW_SEC:
                event = {
                    'kind': 'timeout',
                    'freefall_min': self.freefall_min,
                    'low_ms': self.freefall_low_sec * 1000.0,
                    'max_a': self.wait_max_a,
                    'max_jerk': self.wait_max_jerk,
                }
                self.reset()
                return event
            return None

        if self.state == 'VERIFY_IMPACT':
            self.impact_peak = max(self.impact_peak, a_total)
            self.impact_max_jerk = max(self.impact_max_jerk, jerk)
            elapsed = t_now - self.impact_time
            if elapsed > 0 and a_total <= IMPACT_RELEASE_G:
                event = {
                    'kind': 'fall',
                    'freefall_min': self.freefall_min,
                    'low_ms': self.freefall_low_sec * 1000.0,
                    'impact_peak': self.impact_peak,
                    'impact_jerk': self.impact_max_jerk,
                    'impact_delay_ms': (self.impact_time - self.freefall_armed) * 1000.0,
                    'pulse_ms': elapsed * 1000.0,
                }
                self.reset()
                return event
            if elapsed >= IMPACT_VERIFY_SEC:
                event = {
                    'kind': 'impact_reject',
                    'reason': 'no release below %.2fg within %.0fms'
                              % (IMPACT_RELEASE_G, IMPACT_VERIFY_SEC * 1000),
                    'impact_peak': self.impact_peak,
                    'impact_jerk': self.impact_max_jerk,
                }
                self.reset()
                return event
            return None

        self.reset()
        return None

# ============ main ============
def main():
    clear_fall_alarm_ipc()
    _remove_marker(ALERT_RESOLVING_FILE)
    _recover_interrupted_user_sms()
    print('=' * 50)
    print('K1 Fall Detection (ADXL345)')
    print('Build: %s' % BUILD_ID)
    print('SMS: %s' % ('ENABLED' if SMS_ENABLED else 'DISABLED (--no-sms)'))
    print('Cooldown: %ds' % ACTIVE_COOLDOWN_SEC)
    print('Rule: in %.0fms window accumulate a<%.2fg for >=%.0fms'
          % (FREE_FALL_WINDOW_SEC * 1000, FREE_FALL_THRESH,
             FREE_FALL_ACCUM_SEC * 1000))
    print('      then impact>=%.2fg and jerk>=%.0fg/s within %.1fs'
          % (IMPACT_THRESH, IMPACT_JERK_MIN, FALL_WINDOW_SEC))
    print('      impact must release below %.2fg within %.0fms; no stillness/orientation gate'
          % (IMPACT_RELEASE_G, IMPACT_VERIFY_SEC * 1000))
    print('=' * 50, flush=True)

    # ADXL345
    print('[INIT] ADXL345 @ %s (0x%02X)...' % (I2C_DEV, ADXL_ADDR), flush=True)
    fd = None
    for attempt in range(5):
        try:
            fd = os.open(I2C_DEV, os.O_RDWR)
            fcntl.ioctl(fd, I2C_SLAVE, ADXL_ADDR)
            os.write(fd, bytes([0x00]))
            if os.read(fd, 1)[0] != 0xE5:
                os.close(fd); fd = None; time.sleep(1); continue
            # 100Hz ODR + FULL_RES +/-4g。FULL_RES 下比例仍为约 256 LSB/g。
            os.write(fd, bytes([0x2C, 0x0A]))
            time.sleep(0.01)
            os.write(fd, bytes([0x31, 0x09]))
            time.sleep(0.01)
            os.write(fd, bytes([0x2D, 0x08]))
            time.sleep(0.05)
            print('[INIT] ADXL345 OK attempt=%d' % (attempt+1), flush=True)
            break
        except:
            if fd: os.close(fd); fd = None
            time.sleep(1)
    if fd is None:
        print('[ERR] ADXL345 init FAILED', flush=True); return 1

    SCALE = 1.0 / 256.0

    # ML307A
    print('[INIT] Finding ML307A...', flush=True)
    at_port = find_at_port()
    if at_port:
        print('[INIT] AT port: %s' % at_port, flush=True)
    elif glob.glob('/dev/serial/by-id/*ML307A*if02*'):
        print('[INIT] ML307A AT busy; SMS send will queue and refresh', flush=True)
    else:
        print('[WARN] No ML307A found', flush=True)

    # WS2812B与雷达联动共享。启动时不再绿闪自检，避免覆盖蓝/绿雷达状态。
    print('[INIT] WS2812B: shared with radar; startup self-test skipped', flush=True)
    print('-' * 50, flush=True)
    print('[READY] Monitoring...', flush=True)

    # 主循环
    detector = FallDetector()
    last_fall_time = -ACTIVE_COOLDOWN_SEC
    run = [True]

    def on_sig(sig, frame):
        run[0] = False
    signal.signal(signal.SIGINT, on_sig)
    signal.signal(signal.SIGTERM, on_sig)

    threading.Thread(
        target=_user_sms_worker, args=(run,),
        name='voice-user-sms-worker', daemon=True).start()

    t0 = time.time()
    last_print = 0
    last_at_rescan = 0
    sample_count = 0

    while run[0]:
        try:
            fcntl.ioctl(fd, I2C_SLAVE, ADXL_ADDR)
            os.write(fd, bytes([0x32]))
            ax_raw, ay_raw, az_raw = struct.unpack('<hhh', os.read(fd, 6))
            fax = ax_raw * SCALE; fay = ay_raw * SCALE; faz = az_raw * SCALE
            a_total = math.sqrt(fax*fax + fay*fay + faz*faz)

            t_now = time.time() - t0
            sample_count += 1
            event = detector.update(t_now, a_total)

            if event:
                if event['kind'] == 'armed':
                    print('\n[ARM] low=%.0fms/%.0fms min=%.2fg; waiting impact>=%.2fg'
                          % (event['low_ms'], event['window_ms'],
                             event['freefall_min'], IMPACT_THRESH), flush=True)
                elif event['kind'] == 'timeout':
                    print('\n[TIMEOUT] armed but no impact within %.1fs '
                          '(min=%.2fg low=%.0fms max=%.2fg max_jerk=%.1fg/s)'
                          % (FALL_WINDOW_SEC, event['freefall_min'],
                             event['low_ms'], event['max_a'],
                             event['max_jerk']), flush=True)
                elif event['kind'] == 'impact_candidate':
                    print('\n[IMPACT_CAND] peak=%.2fg jerk=%.1fg/s delay=%.0fms; '
                          'checking pulse release'
                          % (event['impact_peak'], event['impact_jerk'],
                             event['impact_delay_ms']), flush=True)
                elif event['kind'] == 'impact_reject':
                    print('\n[IMPACT_REJECT] peak=%.2fg jerk=%.1fg/s: %s'
                          % (event['impact_peak'], event['impact_jerk'],
                             event['reason']), flush=True)
                elif (event['kind'] == 'fall' and
                      t_now - last_fall_time < ACTIVE_COOLDOWN_SEC):
                    print('\n[FALL] cooldown, skipped (peak=%.2fg jerk=%.1fg/s pulse=%.0fms)'
                          % (event['impact_peak'], event['impact_jerk'],
                             event['pulse_ms']), flush=True)
                elif event['kind'] == 'fall':
                    last_fall_time = t_now
                    print('\n!!! [FALL DETECTED] t=%.1fs !!!' % t_now, flush=True)
                    print('[IMPACT_OK] min=%.2fg low=%.0fms peak=%.2fg '
                          'jerk=%.1fg/s delay=%.0fms pulse=%.0fms'
                          % (event['freefall_min'], event['low_ms'],
                             event['impact_peak'], event['impact_jerk'],
                             event['impact_delay_ms'], event['pulse_ms']), flush=True)

                    at_port = handle_active_fall_alarm(at_port, run)

            # 每10秒重扫ML307A
            if not at_port and t_now - last_at_rescan >= 10:
                last_at_rescan = t_now
                at_port = find_at_port()
                if at_port:
                    print('\n[RECOVER] AT port: %s' % at_port, flush=True)

            if t_now - last_print >= 1.0:
                print('\r[%.0fs] a=%.2fg | samples=%d state=%s'
                      % (t_now, a_total, sample_count, detector.state),
                      end='', flush=True)
                last_print = t_now

            time.sleep(SAMPLE_INTERVAL)

        except OSError as e:
            print('\n[ERR] I2C: errno=%d' % getattr(e, 'errno', 0), flush=True)
            time.sleep(0.5)
            try:
                fcntl.ioctl(fd, I2C_SLAVE, ADXL_ADDR)
            except:
                try: os.close(fd)
                except: pass
                fd = None
                for retry in range(3):
                    time.sleep(1)
                    try:
                        fd = os.open(I2C_DEV, os.O_RDWR)
                        fcntl.ioctl(fd, I2C_SLAVE, ADXL_ADDR)
                        os.write(fd, bytes([0x2D, 0x08]))
                        print('[RECOVER] I2C re-opened', flush=True)
                        break
                    except: pass
                if fd is None:
                    print('[ERR] I2C dead, exiting', flush=True)
                    break

    stop_red_flash()
    clear_fall_alarm_ipc()
    if fd: os.close(fd)
    print('\n[EXIT] Done', flush=True)

if __name__ == '__main__':
    main()
