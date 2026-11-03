"""Small text helpers."""

from __future__ import annotations

import re

_ANSI = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")


def strip_ansi(line: str) -> str:
    return _ANSI.sub("", line)


def split_lines(text: str) -> list[str]:
    """Split on newlines (CRLF normalised), without a phantom last line."""
    text = text.replace("\r\n", "\n")
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines
