"""Stage 1: split raw terminal text into command executions.

This module knows about shell prompts and nothing about errors.

A line is treated as a prompt only if it matches one of a small set of
*anchored* patterns (must start at column 0, must have a recognisable shape
such as ``user@host path %``). Bare ``$``/``%``/``>`` characters inside log
lines never qualify. Users can add their own patterns with ``--prompt-regex``
or the ``CPERR_PROMPT_REGEX`` environment variable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Iterable, Pattern

from .models import Execution, SplitResult
from .textutil import split_lines, strip_ansi

# Optional virtualenv / conda prefix, e.g. "(venv) ".
_ENV = r"(?:\([\w.\-]+\)\s+)?"
_CMD = r"(?:\s+(?P<cmd>\S.*?))?\s*$"

# zsh default: "user@host path % [command]". The path is a single token (or
# absent) and the symbol must be preceded by whitespace and followed by
# whitespace/end-of-line, so "john@example.com is 50% done" cannot match.
_ZSH = re.compile(
    r"^" + _ENV + r"(?P<user>[\w.\-]+)@(?P<host>[\w.\-]+)"
    r"(?:\s+(?P<path>[^\s%$#>]\S*))?\s+(?P<sym>[%$#])(?=\s|$)" + _CMD
)

# bash default: "user@host:path$ [command]"
_BASH = re.compile(
    r"^" + _ENV + r"(?P<user>[\w.\-]+)@(?P<host>[\w.\-]+):(?P<path>[~/]\S*?)\s?(?P<sym>[$#])(?=\s|$)" + _CMD
)

# RHEL style: "[user@host path]$ [command]"
_BRACKET = re.compile(
    r"^" + _ENV + r"\[(?P<user>[\w.\-]+)@(?P<host>[\w.\-]+)\s+[^\]]+\]\s?(?P<sym>[$#%])(?=\s|$)" + _CMD
)

# zsh/bash with only a path in the prompt: "~/dir %", "/usr/local $"
_PATH_ONLY = re.compile(
    r"^" + _ENV + r"(?P<path>(?:~|/)[^\s%$#>]*)\s+(?P<sym>[%$#])(?=\s|$)" + _CMD
)

# oh-my-zsh robbyrussell: "➜  dir git:(main) ✗ cmd"; starship/pure: "❯ cmd"
_FANCY_ARROW = re.compile(r"^➜\s+(?P<path>\S+)(?:\s+git:\([^)]*\))?(?:\s+[✗✔])?" + _CMD)
_FANCY_CHEVRON = re.compile(r"^[❯λ](?:\s+(?P<cmd>\S.*?))?\s*$")

DEFAULT_PROMPT_PATTERNS: tuple[Pattern[str], ...] = (
    _ZSH,
    _BASH,
    _BRACKET,
    _PATH_ONLY,
    _FANCY_ARROW,
    _FANCY_CHEVRON,
)


@dataclass(frozen=True)
class PromptMatch:
    command: str | None


def compile_user_patterns(patterns: Iterable[str]) -> tuple[Pattern[str], ...]:
    """Compile user-supplied prompt regexes. Raises ``ValueError`` on bad input."""
    out = []
    for p in patterns:
        try:
            out.append(re.compile(p))
        except re.error as exc:
            raise ValueError(f"invalid --prompt-regex {p!r}: {exc}") from exc
    return tuple(out)


def match_prompt(
    line: str, patterns: tuple[Pattern[str], ...] = DEFAULT_PROMPT_PATTERNS
) -> PromptMatch | None:
    """Return a match if ``line`` looks like a shell prompt (column 0 only)."""
    if not line or line[0].isspace():
        return None
    for pat in patterns:
        m = pat.match(line)
        if m:
            cmd = m.groupdict().get("cmd")
            return PromptMatch(command=cmd.strip() if cmd else None)
    return None


def split_executions(
    text: str,
    extra_patterns: tuple[Pattern[str], ...] = (),
    use_default_patterns: bool = True,
) -> SplitResult:
    """Split ``text`` into executions delimited by shell prompts.

    * A prompt followed by no output (blank lines only) produces no execution,
      so a trailing empty prompt never adds one.
    * Text before the first prompt becomes an execution flagged
      ``partial_start`` (its own prompt was not copied).
    * With no recognised prompt at all the whole text is one execution and
      ``boundaries_known`` is False.
    """
    patterns = extra_patterns + (DEFAULT_PROMPT_PATTERNS if use_default_patterns else ())
    raw = split_lines(text)
    scan = [strip_ansi(l) for l in raw]

    prompt_idx: list[int] = []
    prompt_info: dict[int, PromptMatch] = {}
    for i, s in enumerate(scan):
        pm = match_prompt(s, patterns)
        if pm:
            prompt_idx.append(i)
            prompt_info[i] = pm

    executions: list[Execution] = []

    def add(start: int, end: int, prompt_i: int | None, partial: bool) -> None:
        # [start, end) output lines; drop trailing blank lines.
        while end > start and not scan[end - 1].strip():
            end -= 1
        if end <= start or not any(scan[k].strip() for k in range(start, end)):
            return
        executions.append(
            Execution(
                index=len(executions) + 1,
                lines=tuple(raw[start:end]),
                scan_lines=tuple(scan[start:end]),
                first_line_no=start + 1,
                prompt=scan[prompt_i] if prompt_i is not None else None,
                command=prompt_info[prompt_i].command if prompt_i is not None else None,
                partial_start=partial,
            )
        )

    if not prompt_idx:
        add(0, len(raw), None, True)
        return SplitResult(tuple(executions), boundaries_known=False)

    add(0, prompt_idx[0], None, True)
    for n, pi in enumerate(prompt_idx):
        nxt = prompt_idx[n + 1] if n + 1 < len(prompt_idx) else len(raw)
        add(pi + 1, nxt, pi, False)

    return SplitResult(
        tuple(executions),
        boundaries_known=True,
        prompt_lines=tuple(i + 1 for i in prompt_idx),
    )
