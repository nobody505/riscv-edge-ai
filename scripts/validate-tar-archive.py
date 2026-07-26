#!/usr/bin/env python3
"""Reject archive members that could escape an extraction directory."""

from __future__ import annotations

import argparse
import posixpath
import tarfile
from pathlib import PurePosixPath


def is_safe_path(value: str) -> bool:
    path = PurePosixPath(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts


def validate_archive(path: str) -> None:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if not is_safe_path(member.name):
                raise ValueError(f"unsafe archive member: {member.name!r}")
            if member.issym() or member.islnk():
                target = member.linkname
                if member.issym():
                    target = posixpath.join(posixpath.dirname(member.name), target)
                if not is_safe_path(posixpath.normpath(target)):
                    raise ValueError(
                        f"unsafe archive link: {member.name!r} -> {member.linkname!r}"
                    )
            if member.isdev() or member.isfifo():
                raise ValueError(f"special archive member is not allowed: {member.name!r}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("archive")
    args = parser.parse_args()
    validate_archive(args.archive)
    print(f"archive paths OK: {args.archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
