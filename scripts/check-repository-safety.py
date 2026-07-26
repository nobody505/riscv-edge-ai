#!/usr/bin/env python3
"""Fail when tracked source contains likely credentials or personal data."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
BINARY_SUFFIXES = {
    ".bin", ".gz", ".hd", ".mvn", ".onnx", ".png", ".wav",
}
EMAIL_EXCLUDED_PREFIXES = ("voice/vendor/",)

RULES = {
    "windows-user-profile": re.compile(
        r"\b[A-Z]:[\\/]Users[\\/][^\\/\s]+[\\/]", re.IGNORECASE
    ),
    "local-windows-worktree": re.compile(
        r"\b[A-Z]:\\(?:Python|Projects|Documents)\\", re.IGNORECASE
    ),
    "private-ipv4": re.compile(
        r"\b(?:10\.(?:\d{1,3}\.){2}\d{1,3}"
        r"|192\.168\.\d{1,3}\.\d{1,3}"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})\b"
    ),
    "device-hostname": re.compile(
        r"\b[a-z0-9][a-z0-9_-]*-spacemit[\w.-]*\b", re.IGNORECASE
    ),
    "mobile-number": re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    "wechat-app-id": re.compile(r"\bwx[0-9a-f]{16}\b", re.IGNORECASE),
    "dcloud-app-id": re.compile(r"\b__UNI__[0-9a-f]{7,}\b", re.IGNORECASE),
    "email-address": re.compile(
        r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\."
        r"(?:com|cn|net|org|edu|gov|io|dev|ai|me|co|info|xyz|top|tech)\b",
        re.IGNORECASE,
    ),
}


def repository_files() -> list[str]:
    result = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard", "-z"],
        cwd=ROOT,
        check=True,
        stdout=subprocess.PIPE,
    )
    return sorted(item.decode("utf-8") for item in result.stdout.split(b"\0") if item)


def main() -> int:
    findings: list[tuple[str, int, str]] = []
    for relative in repository_files():
        path = ROOT / relative
        if not path.is_file() or path.suffix.lower() in BINARY_SUFFIXES:
            continue
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            continue
        text = data.decode("utf-8", errors="replace")
        for line_number, line in enumerate(text.splitlines(), start=1):
            for rule_name, pattern in RULES.items():
                if (
                    rule_name == "email-address"
                    and relative.startswith(EMAIL_EXCLUDED_PREFIXES)
                ):
                    continue
                if pattern.search(line):
                    findings.append((relative, line_number, rule_name))

    if findings:
        for relative, line_number, rule_name in findings:
            print(f"{relative}:{line_number}: blocked by {rule_name}")
        print(f"repository safety scan failed with {len(findings)} finding(s)")
        return 1

    print("repository safety scan passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
