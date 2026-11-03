"""macOS clipboard access via pbpaste / pbcopy (subprocess only)."""

from __future__ import annotations

import os
import subprocess

TIMEOUT = 10  # seconds


class ClipboardError(RuntimeError):
    """The clipboard could not be read or written."""


def _env() -> dict[str, str]:
    # pbcopy/pbpaste decode using the locale; force UTF-8 so non-ASCII
    # survives even when invoked from a stripped-down environment.
    env = dict(os.environ)
    env["LANG"] = "en_US.UTF-8"
    env.pop("LC_ALL", None)
    return env


def _run(cmd: list[str], data: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            cmd, input=data, capture_output=True, timeout=TIMEOUT, env=_env()
        )
    except FileNotFoundError:
        raise ClipboardError(
            f"'{cmd[0]}' not found. cperr needs macOS clipboard utilities "
            f"(pbcopy/pbpaste) on PATH; use --stdin to read from a pipe instead."
        ) from None
    except subprocess.TimeoutExpired:
        raise ClipboardError(f"'{cmd[0]}' timed out after {TIMEOUT}s") from None
    except OSError as exc:
        raise ClipboardError(f"could not run '{cmd[0]}': {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()
        raise ClipboardError(
            f"'{cmd[0]}' exited with status {proc.returncode}" + (f": {detail}" if detail else "")
        )
    return proc.stdout


def read_clipboard() -> str:
    return _run(["pbpaste"]).decode("utf-8", "replace")


def write_clipboard(text: str) -> None:
    _run(["pbcopy"], text.encode("utf-8"))
