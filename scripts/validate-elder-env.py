#!/usr/bin/env python3
"""Validate the exact systemd EnvironmentFile schema without echoing secrets."""

from __future__ import annotations

import argparse
import base64
import binascii
import re
from pathlib import Path


EXPECTED_KEYS = {
    "ONENET_PRODUCT_ID",
    "ONENET_DEVICE_NAME",
    "ONENET_DEVICE_KEY_B64",
    "ONENET_PRODUCT_KEY_B64",
    "TENCENT_MAP_KEY",
    "ELDER_SMS_PHONE",
    "ELDER_SMS_CONTACT_NAME",
}
KEY_LINE = re.compile(r"^([A-Z][A-Z0-9_]*)=(.*)$")
IDENTIFIER = re.compile(r"^[\w.-]{1,128}$", re.UNICODE)
MAP_KEY = re.compile(r"^[A-Za-z0-9_-]{8,128}$")
PHONE = re.compile(r"^1[3-9][0-9]{9}$")
CONTACT_NAME = re.compile(r"^[\w\u3400-\u9fff .-]{1,32}$", re.UNICODE)


class ConfigurationError(ValueError):
    pass


def parse_environment(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line_number, raw_line in enumerate(
        path.read_text(encoding="utf-8").splitlines(), start=1
    ):
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = KEY_LINE.fullmatch(line)
        if not match:
            raise ConfigurationError(f"invalid assignment at line {line_number}")
        key, value = match.groups()
        if key not in EXPECTED_KEYS:
            raise ConfigurationError(f"unknown configuration key: {key}")
        if key in values:
            raise ConfigurationError(f"duplicate configuration key: {key}")
        if not value or "REPLACE_WITH" in value:
            raise ConfigurationError(f"empty or placeholder value: {key}")
        values[key] = value
    missing = EXPECTED_KEYS - values.keys()
    if missing:
        raise ConfigurationError(
            "missing configuration key(s): " + ", ".join(sorted(missing))
        )
    return values


def validate_base64_key(name: str, value: str) -> None:
    try:
        decoded = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError) as error:
        raise ConfigurationError(f"invalid Base64 value: {name}") from error
    if not 8 <= len(decoded) <= 256:
        raise ConfigurationError(f"unexpected decoded key length: {name}")


def validate_environment(path: Path) -> None:
    values = parse_environment(path)
    for name in ("ONENET_PRODUCT_ID", "ONENET_DEVICE_NAME"):
        if not IDENTIFIER.fullmatch(values[name]):
            raise ConfigurationError(f"invalid identifier: {name}")
    for name in ("ONENET_DEVICE_KEY_B64", "ONENET_PRODUCT_KEY_B64"):
        validate_base64_key(name, values[name])
    if not MAP_KEY.fullmatch(values["TENCENT_MAP_KEY"]):
        raise ConfigurationError("invalid map key format: TENCENT_MAP_KEY")
    if not PHONE.fullmatch(values["ELDER_SMS_PHONE"]):
        raise ConfigurationError("invalid mobile number: ELDER_SMS_PHONE")
    if not CONTACT_NAME.fullmatch(values["ELDER_SMS_CONTACT_NAME"]):
        raise ConfigurationError("invalid contact name: ELDER_SMS_CONTACT_NAME")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("environment_file", type=Path)
    args = parser.parse_args()
    try:
        validate_environment(args.environment_file)
    except (OSError, UnicodeError, ConfigurationError) as error:
        parser.error(str(error))
    print("elder.env schema OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
