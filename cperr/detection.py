"""Stage 2: find logical error occurrences inside ONE execution's output.

This module knows nothing about shell prompts or context windows. It receives
plain lines and returns :class:`~cperr.models.ErrorBlock` objects.

Extending
---------
Detection is table-driven. A :class:`Rule` has a ``match(lines, i)`` callable
that returns the *inclusive* end index of an error that starts at line ``i``
(or ``None``). Add a rule by appending to ``DEFAULT_RULES`` (order matters:
the first rule that matches wins), or pass your own list to
:func:`detect_errors`. For a simple one-line format use :func:`line_rule`::

    line_rule("my-tool", r"^mytool: (fatal|error)\\b")

Rule roles
----------
``strong``   a real error occurrence; always counted.
``summary``  a line that *summarises* failure ("Scan failed", "exit status 1",
             "BUILD FAILED"). If it appears within ``SUMMARY_GAP`` lines after
             a detected block it is absorbed into that block (so it is not
             counted twice). Otherwise it is counted only if the rule has
             ``counts_alone=True``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from typing import Callable, Optional, Pattern, Sequence

from .models import ErrorBlock

Lines = Sequence[str]
Matcher = Callable[[Lines, int], Optional[int]]

SUMMARY_GAP = 4  # max (i - previous_block.end) for a summary line to be absorbed


@dataclass(frozen=True)
class Rule:
    name: str
    match: Matcher
    role: str = "strong"  # "strong" | "summary"
    counts_alone: bool = True  # summary rules only
    multiline: bool = False  # may span several lines (traceback, stack trace)
    level_filter: bool = True  # skip lines that are clearly INFO/DEBUG/WARN
    description: str = ""


# --------------------------------------------------------------------------
# Global guards for single-line rules
# --------------------------------------------------------------------------

# Line is a log entry of non-error severity, e.g. "INFO: ...", "[WARN] ...",
# "2024-01-01 12:00:00 DEBUG ...".
_LOW_LEVEL = re.compile(
    r"^\W*(?:\d[\d\-:.,T/ +Z]*\s+)?[\[(<]?(?:INFO|DEBUG|TRACE|NOTICE|WARN(?:ING)?)\b[\])>]?",
    re.IGNORECASE,
)
_LOW_LEVEL_TOKEN = re.compile(r"\b(?:INFO|DEBUG|TRACE|NOTICE|WARN|WARNING)\b")

# "0 errors", "no errors", "errors: 0", "failed=0" ...
_NEGATED = re.compile(
    r"\b(?:no|zero|0|without)\s+(?:\w+\s+)?(?:errors?|failures?|failed)\b"
    r"|\b(?:errors?|failures?|failed)\s*[:=]\s*0\b"
    r"|\b0\s+(?:errors?|failed|failures?)\b",
    re.IGNORECASE,
)


def _suppressed(line: str) -> bool:
    return bool(_LOW_LEVEL.match(line) or _NEGATED.search(line))


# --------------------------------------------------------------------------
# Rule constructors
# --------------------------------------------------------------------------


def line_rule(
    name: str,
    pattern: str | Pattern[str],
    *,
    flags: int = re.IGNORECASE,
    role: str = "strong",
    counts_alone: bool = True,
    level_filter: bool = True,
    description: str = "",
) -> Rule:
    """Build a one-line rule from a regex (searched anywhere in the line)."""
    rx = re.compile(pattern, flags) if isinstance(pattern, str) else pattern

    def match(lines: Lines, i: int) -> Optional[int]:
        return i if rx.search(lines[i]) else None

    return Rule(name, match, role, counts_alone, False, level_filter, description)


# --------------------------------------------------------------------------
# Multi-line rules
# --------------------------------------------------------------------------

_PY_HDR = re.compile(r"^Traceback \(most recent call last\):\s*$")
_PY_CHAIN = re.compile(
    r"^(?:During handling of the above exception, another exception occurred:"
    r"|The above exception was the direct cause of the following exception:)\s*$"
)
_PY_EXC_COLON = re.compile(r"^(?:[A-Za-z_]\w*\.)*[A-Z]\w*[a-z]\w*:(?:\s.*)?$")
_PY_EXC_BARE = re.compile(
    r"^(?:[A-Za-z_]\w*\.)*[A-Z]\w*(?:Error|Exception|Exit|Interrupt|Iteration|Warning)$"
)


def _is_indented(line: str) -> bool:
    return line[:1] in (" ", "\t") and bool(line.strip())


def python_traceback(lines: Lines, i: int) -> Optional[int]:
    """``Traceback`` header, frames, terminal exception, and chained tracebacks."""
    if not _PY_HDR.match(lines[i]):
        return None
    n = len(lines)
    j, end = i + 1, i
    while True:
        while j < n and _is_indented(lines[j]):
            j += 1
        end = max(end, j - 1)
        if j < n and (_PY_EXC_COLON.match(lines[j]) or _PY_EXC_BARE.match(lines[j])):
            end = j
            j += 1
        k = j
        while k < n and not lines[k].strip():
            k += 1
        if k < n and _PY_CHAIN.match(lines[k]):
            m = k + 1
            while m < n and not lines[m].strip():
                m += 1
            if m < n and _PY_HDR.match(lines[m]):
                end, j = m, m + 1
                continue
        return end


_EXC_NAME = r"(?:[\w$]+\.)*(?:[A-Z][\w$]*)?(?:Error|Exception|Throwable)"
_EXC_HDR = re.compile(
    r'^(?:Exception in thread "[^"]*"\s+)?(?:(?:Uncaught|Unhandled)\s+)?'
    + _EXC_NAME
    + r"(?::(?:\s.*)?)?$"
    r"|^Caused by: \S.*$"
)
_STACK_CONT = re.compile(
    r"^\s+at\s+\S"  # Java / JS frame
    r"|^\s*\.\.\.\s+\d+\s+(?:more|common frames omitted|lines? matching)"
    r"|^\s*(?:Caused by|Suppressed):\s"
    r"|^\s*\[cause\]:"
    r"|^Node\.js v\d"
)


def exception_trace(lines: Lines, i: int) -> Optional[int]:
    """Exception header (JVM / Node / Python-style), plus frames and causes.

    Also matches a lone ``SomeError: message`` line (a one-line block).
    """
    if not _EXC_HDR.match(lines[i]):
        return None
    n = len(lines)
    end = i
    j = i + 1
    while j < n:
        line = lines[j]
        if _STACK_CONT.match(line):
            end = j
            j += 1
            # Node prints error properties as an object after the last frame.
            if line.rstrip().endswith("{"):
                k = j
                while k < n and lines[k].strip() != "}":
                    k += 1
                if k < n:
                    end, j = k, k + 1
            continue
        break
    return end


_GO_HDR = re.compile(r"^(?:panic|fatal error|runtime error):\s")
_GO_CONT = re.compile(
    r"^goroutine \d+ \["
    r"|^created by "
    r"|^exit status \d+"
    r"|^\[signal "
    r"|^[A-Za-z_][\w./*()\[\]{}-]*\(.*\)$"
)


def go_panic(lines: Lines, i: int) -> Optional[int]:
    if not _GO_HDR.match(lines[i]):
        return None
    n = len(lines)
    end, j = i, i + 1
    while j < n:
        line = lines[j]
        if not line.strip():
            j += 1
            continue
        if line[:1] in (" ", "\t") or _GO_CONT.match(line):
            end = j
            j += 1
            continue
        break
    return end


def _run_rule(pattern: str) -> Matcher:
    """Consecutive lines that all match ``pattern`` form ONE failure."""
    rx = re.compile(pattern)

    def match(lines: Lines, i: int) -> Optional[int]:
        if not rx.match(lines[i]):
            return None
        j = i
        while j + 1 < len(lines) and rx.match(lines[j + 1]):
            j += 1
        return j

    return match


npm_error_run = _run_rule(r"npm (?:ERR!|error\b)")
maven_error_run = _run_rule(r"\[ERROR\]")
pytest_assert_run = _run_rule(r"E\s{2,}\S")


# --------------------------------------------------------------------------
# Single-line matchers needing a little more logic than a regex
# --------------------------------------------------------------------------

_LOG_LEVEL = re.compile(
    r"(?<![\w.])(?:ERROR|FATAL|CRITICAL|SEVERE|FAILED|FAILURE|PANIC)(?!\w)"
)


def log_level(lines: Lines, i: int) -> Optional[int]:
    """Upper-case severity token (ERROR, FATAL, CRITICAL, FAILED ...)."""
    line = lines[i]
    m = _LOG_LEVEL.search(line)
    if not m or "│" in line or "┃" in line:  # table rows from scanners
        return None
    if _LOW_LEVEL_TOKEN.search(line[: m.start()]):
        return None
    return i


# --------------------------------------------------------------------------
# Default rule set
# --------------------------------------------------------------------------

_I = re.IGNORECASE


def _alt(*parts: str) -> str:
    return "|".join(parts)


DEFAULT_RULES: tuple[Rule, ...] = (
    # ---- multi-line blocks -------------------------------------------------
    Rule("python-traceback", python_traceback, multiline=True,
         description="Traceback header, frames, terminal exception, chained tracebacks"),
    Rule("exception-trace", exception_trace, multiline=True,
         description="JVM/Node/Python exception line with at-frames and Caused by"),
    Rule("go-panic", go_panic, multiline=True,
         description="Go panic / fatal error with goroutine trace"),
    Rule("npm-error-run", npm_error_run, multiline=True,
         description="Consecutive npm ERR! lines"),
    Rule("maven-error-run", maven_error_run, multiline=True,
         description="Consecutive [ERROR] lines (Maven)"),
    Rule("pytest-assert", pytest_assert_run, multiline=True,
         description="Consecutive 'E   ...' lines of a pytest failure"),
    # ---- summaries that contain strong tokens (must precede log-level) -------
    line_rule("test-run-summary", r"^(?:FAILED|FAIL|ERROR)\s*\((?:failures|errors)=\d",
              flags=0, role="summary", counts_alone=False, level_filter=False,
              description="unittest 'FAILED (errors=1)' footer"),
    line_rule("build-summary", r"\bBUILD (?:FAILED|FAILURE)\b|^FAILURE: Build failed",
              flags=0, role="summary", counts_alone=True, level_filter=False,
              description="Gradle / Maven build failure banner"),
    # ---- generic log entries -----------------------------------------------
    Rule("log-level", log_level, description="ERROR/FATAL/CRITICAL/SEVERE/FAILED token"),
    line_rule("structured-level",
              r"""\b(?:level|lvl|severity)["']?\s*[=:]\s*["']?(?:error|eror|fatal|critical|crit|severe)\b""",
              description="level=error / \"level\":\"error\""),
    line_rule("bracket-level", r"[\[<](?:error|crit|alert|emerg)[\]>]",
              description="[error] style (nginx, syslog)"),
    line_rule("tool-prefix",
              r"^\s*(?:\S+:\s+)?(?:fatal|error|err)(?:\[\w+\])?:\s|:\d+(?::\d+)?:\s*(?:fatal )?error\b|^E: ",
              description="git 'fatal:', rustc 'error[E0425]:', gcc 'file:1:2: error', apt 'E:'"),
    line_rule("unittest-fail", r"^FAIL: \w+ \(", flags=0, description="unittest FAIL header"),
    line_rule("ci-annotation", r"^##\[error\]|^::error",
              description="GitHub Actions / Azure DevOps error annotations"),
    # ---- shell / OS --------------------------------------------------------
    line_rule("shell-failure", _alt(
        r"command not found", r"\bpermission denied\b", r"operation not permitted",
        r"access is denied", r"no such file or directory", r"\bsegmentation fault\b",
        r"\bbus error\b", r"\billegal instruction\b", r"\babort trap\b", r"\bcore dumped\b",
        r"^Killed(?::\s*\d+)?$", r"\bbad substitution\b", r"\bunbound variable\b",
        r"\bsyntax error near\b", r"\bis not recognized as an internal or external command\b",
        r"\bno space left on device\b", r"\bout of memory\b", r"\bcannot allocate memory\b",
    ), description="command not found, permission denied, ENOENT, crashes ..."),
    # ---- containers / orchestration ------------------------------------------
    line_rule("docker", _alt(
        r"error response from daemon", r"cannot connect to the docker daemon",
        r"\bfailed to solve\b", r"pull access denied", r"manifest unknown",
        r"no matching manifest", r"\bexec format error\b", r"\bOCI runtime\b.*(?:error|failed)",
        r"executor failed running", r"did not complete successfully",
        r"requested access to the resource is denied", r"\btoomanyrequests\b",
    ), description="Docker daemon / build / registry failures"),
    line_rule("kubernetes", _alt(
        r"\b(?:CrashLoopBackOff|ImagePullBackOff|ErrImagePull|ErrImageNeverPull|"
        r"CreateContainerConfigError|CreateContainerError|InvalidImageName|OOMKilled|"
        r"Evicted|FailedScheduling|FailedMount|FailedCreate)\b",
        r"error from server", r"unable to connect to the server",
        r"the connection to the server .* was refused", r"back-off restarting failed container",
        r"failed to pull image", r"(?:readiness|liveness|startup) probe failed",
        r"^│\s*Error:",
    ), flags=0 | re.IGNORECASE, description="kubectl / helm / terraform failures"),
    # ---- HTTP / network ------------------------------------------------------
    line_rule("http-network", _alt(
        r"\bHTTP/[\d.]+\s+[45]\d\d\b",
        r"\b(?:status(?:[ _]code)?|http)\s*[:=]?\s*\"?[45]\d\d\b",
        r"\b[45]\d\d\s+(?:Bad Request|Unauthorized|Payment Required|Forbidden|Not Found|"
        r"Method Not Allowed|Request Timeout|Conflict|Too Many Requests|Internal Server Error|"
        r"Bad Gateway|Service Unavailable|Gateway Time-?out)\b",
        r"^curl: \(\d+\)",
        r"\b(?:ECONNREFUSED|ECONNRESET|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|EHOSTUNREACH|EADDRINUSE|ENOENT|EACCES|EPERM)\b",
        r"connection (?:refused|reset by peer|timed out)", r"could not resolve host",
        r"name or service not known", r"temporary failure in name resolution",
        r"ssl certificate problem", r"certificate verify failed", r"\btimed out\b",
    ), description="HTTP 4xx/5xx, curl errors, ECONN*, DNS and TLS failures"),
    # ---- security scanners -----------------------------------------------------
    line_rule("security-scanner", _alt(
        r"^Total: [1-9]\d* \(.*\b(?:HIGH|CRITICAL): [1-9]",
        r"\b[1-9]\d* (?:high|critical)(?: severity)? vulnerabilit",
        r"\bfound [1-9]\d* vulnerabilit", r"vulnerabilities \(.*\b(?:high|critical)\b",
        r"severity vulnerability found", r"^FAIL-(?:NEW|INPROG): [1-9]",
        r"quality gate .*(?:failed|error)",
    ), description="Trivy / npm audit / Snyk / ZAP / Sonar summaries"),
    # ---- build diagnostics / phrases -----------------------------------------
    line_rule("build-diagnostic", _alt(
        r"cannot find module", r"no module named", r"cannot find symbol",
        r"undefined reference to", r"unresolved import", r"cannot find package",
        r"could not (?:find|resolve) (?:artifact|dependency|a version)",
    ), description="missing module / symbol / dependency"),
    line_rule("failure-phrase", _alt(
        r"\bfailed (?:to|with|at|while|during)\b",
        r"\b(?:unable|could not|couldn't|cannot|can't) (?:to )?(?:connect|open|find|read|write|"
        r"resolve|load|create|access|bind|start|parse|locate|authenticate|import|execute)\b",
    ), description="'failed to ...', 'unable to connect' phrases"),
    # ---- weaker summaries (absorbed into a nearby block, else mostly ignored) ----
    line_rule("exit-status",
              _alt(r"\bexit(?:ed)?(?: with)?(?: code| status)[: ]+[1-9]\d*\b",
                   r"non-?zero exit", r"^exit status [1-9]\d*$",
                   r"^make: \*\*\*", r"process completed with exit code [1-9]"),
              role="summary", counts_alone=True,
              description="non-zero exit diagnostics"),
    line_rule("failure-word", r"\b(?:failed|failures?|aborted|unsuccessful)\b",
              role="summary", counts_alone=False,
              description="generic 'X failed' (absorbed or final-line fallback only)"),
)


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

_SEPARATOR = re.compile(r"^\s*[-=]{5,}\s*$")


def _first_match(rules: Sequence[Rule], lines: Lines, i: int) -> Optional[tuple[Rule, int]]:
    line = lines[i]
    suppressed = _suppressed(line)
    for rule in rules:
        if suppressed and not rule.multiline and rule.level_filter:
            continue
        end = rule.match(lines, i)
        if end is not None:
            return rule, end
    return None


def _headline(line: str) -> str:
    s = line.strip()
    return s if len(s) <= 120 else s[:117] + "..."


def detect_errors(lines: Lines, rules: Sequence[Rule] = DEFAULT_RULES) -> list[ErrorBlock]:
    """Return logical error occurrences in order of appearance."""
    n = len(lines)
    blocks: list[ErrorBlock] = []
    fallback: Optional[tuple[Rule, int]] = None
    last_nonblank = max((k for k in range(n) if lines[k].strip()), default=-1)
    detail_rules = [r for r in rules if r.multiline]

    i = 0
    while i < n:
        if not lines[i].strip():
            i += 1
            continue
        hit = _first_match(rules, lines, i)
        if hit is None:
            i += 1
            continue
        rule, end = hit

        if rule.role == "summary":
            if blocks and i - blocks[-1].end <= SUMMARY_GAP:
                blocks[-1] = replace(blocks[-1], end=max(blocks[-1].end, end))
            elif rule.counts_alone:
                blocks.append(ErrorBlock(i, end, rule.name, _headline(lines[i])))
            elif i == last_nonblank:
                fallback = (rule, i)
            i = end + 1
            continue

        if not rule.multiline:
            # A log line immediately followed by a stack trace is ONE occurrence.
            d = end + 1
            if d < n and _SEPARATOR.match(lines[d]):
                d += 1
            if d < n:
                for dr in detail_rules:
                    dend = dr.match(lines, d)
                    if dend is not None and dend > d:
                        end = dend
                        break
        blocks.append(ErrorBlock(i, end, rule.name, _headline(lines[i])))
        i = end + 1

    if not blocks and fallback is not None:
        rule, k = fallback
        blocks.append(ErrorBlock(k, k, "failure-word-fallback", _headline(lines[k])))
    return blocks
