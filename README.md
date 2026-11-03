# cperr

Extract the useful error(s) from copied terminal output, drop the noise, and put the
cleaned result back on the macOS clipboard - ready to paste into a ticket, chat, or
an AI assistant.

* Python 3.11+, **standard library only**, no network access, nothing is sent anywhere.
* Reads with `pbpaste`, writes with `pbcopy` (or reads stdin with `--stdin`).
* Summaries go to **stderr**; extracted text goes to **stdout**, so it pipes cleanly.

## Install

```zsh
cd /path/to/cperr
./install.sh                 # installs launcher to ~/.local/bin/cperr
source ~/.zshrc              # only needed if install.sh added ~/.local/bin to PATH
cperr --help
```

`install.sh` writes a two-line launcher that runs `bin/cperr` with the Python 3.11+
interpreter it finds at install time (macOS's system `/usr/bin/python3` is too old on
many machines), so `cperr` works from any directory. Keep the source folder in place;
the launcher points at it.

Options: `./install.sh --prefix /usr/local/bin` (may need `sudo`), `./install.sh --uninstall`.

Alternative: `pipx install .` (uses the `cperr` console script from `pyproject.toml`).

Run the tests (no clipboard needed):

```zsh
python3 -m unittest discover -s tests -t .
```

## Usage

Copy some terminal output (Cmd-C), then:

| Command | What it does |
|---|---|
| `cperr` | Summary + latest error with 10 lines of context; copies result |
| `cperr -r 2 -l 22` | Latest 2 errors, up to 22 lines above and below each |
| `cperr --count` | Error counts per execution only; never touches the clipboard |
| `cperr --run 1` | Only the most recent execution (1 = latest, 2 = the one before ...) |
| `cperr --run 2 -r 3 -l 10` | Up to 3 latest errors from the second-most-recent execution |
| `cperr --all` | Every detected error from every execution (or from `--run N`) |
| `cperr --stdin -r 2 -l 22` | Read stdin instead of the clipboard, e.g. `make 2>&1 \| cperr --stdin` |
| `cperr --dry-run` | Print the result; leave the clipboard alone |
| `cperr --verbose` | Show prompt boundaries, each detected block + rule, output ranges |

Other options: `-n/--line-numbers`, `-q/--quiet`, `--prompt-regex REGEX` (repeatable),
`--version`. Env var `CPERR_PROMPT_REGEX` holds extra prompt regexes, one per line.

Defaults: `-r 1`, `-l 10`, newest errors first.

Exit status: `0` success, `1` no errors / empty input (clipboard left unchanged),
`2` bad arguments, `3` clipboard failure.

### Example

Clipboard contents:

```text
akashshukla@Akashs-MacBook-Pro scan-reports % ./scan.sh
INFO: Starting scan
ERROR: Connection refused
Traceback (most recent call last):
  File "scan.py", line 3, in <module>
    run()
ConnectionError: refused
ERROR: Retry failed
Scan failed

akashshukla@Akashs-MacBook-Pro scan-reports % ./scan.sh
INFO: Starting scan
ERROR: Timeout
Scan failed

akashshukla@Akashs-MacBook-Pro scan-reports %
```

`cperr -r 2 -l 1` prints to stderr:

```text
Executions detected: 2
  Note: only the copied text is analysed; earlier shell history is not included.

Execution 1 (oldest, --run 2) : 2 errors
Execution 2 (latest, --run 1) : 1 error

Total detected errors: 3
Selected error occurrences: 2
Context lines: 1 above and below
Output sections: 2 (overlapping or adjacent ranges: merged)
```

(Execution 1 has two errors: the traceback block counts once, `ERROR: Retry failed`
once; the `Connection refused` log line is absorbed into the traceback that
follows it. The trailing empty prompt creates no execution.)

and to stdout (and the clipboard), oldest first, bottom-up selected:

```text
=== Execution 1 of 2 (--run 2, oldest) - ./scan.sh ===
ConnectionError: refused
ERROR: Retry failed
Scan failed

=== Execution 2 of 2 (--run 1, latest) - ./scan.sh ===
INFO: Starting scan
ERROR: Timeout
Scan failed
```

Execution headers appear only when more than one execution is in the output. Between
disjoint ranges of one execution a `... [lines omitted] ...` separator is inserted.

## How it works

Three independent stages (`cperr/`):

| Module | Responsibility |
|---|---|
| `executions.py` | Split text on shell prompts into executions. Knows nothing about errors. |
| `detection.py` | Find logical error blocks in one execution's lines. Knows nothing about prompts or context. |
| `extraction.py` | Bottom-up selection, +/- context, clamp, sort, merge ranges. |
| `report.py` / `cli.py` / `clipboard.py` | Formatting, argument handling, `pbcopy`/`pbpaste`. |
| `models.py` | Immutable dataclasses passed between stages (`Execution`, `ErrorBlock`, `Analysis`, ...). |

### Prompt boundary heuristics

A line is a prompt only if it starts in column 0 **and** matches an anchored shape:
`user@host path %`, `user@host:path$`, `[user@host dir]#`, `~/dir %`, oh-my-zsh `➜  dir`,
`❯`, with optional `(venv) ` prefix. Usernames and hostnames are never hardcoded.
A bare `%`, `$` or `>` in a log line (`Progress: 50%`, `> quoted`, `john@example.com is 50% done`)
is not a prompt. Add your own with `--prompt-regex` / `CPERR_PROMPT_REGEX`.

* A prompt with no output after it (e.g. the trailing one) creates no execution.
* Text before the first prompt is an execution flagged as probably incomplete.
* With no prompt recognised, everything is one execution and the summary says
  boundaries are **unknown**.
* The tool only sees the copied text. It does not know about earlier shell history.

### Error detection heuristics

Rules are an ordered table in `detection.py` (first match wins). Multi-line rules:
Python tracebacks (including chained ones), JVM/Node exceptions with `at` frames and
`Caused by:`, Go panics with goroutine traces, and runs of `npm ERR!`, Maven `[ERROR]`,
or pytest `E   ` lines. Single-line rules: `ERROR`/`FATAL`/`CRITICAL`/`FAILED` tokens,
`level=error`, `[error]`, tool prefixes (`fatal:`, `error[E0425]:`, `file:1:2: error`),
shell failures (`command not found`, `permission denied`), Docker, Kubernetes, HTTP 4xx/5xx
and network errors, security-scanner summaries, and missing-module/dependency messages.

Grouping and noise rules:

* A traceback header, frames, chained causes and the final exception line = **one** block.
* An error log line followed directly by a stack trace = **one** block.
* A "summary" line (`Scan failed`, `exit status 1`, `BUILD FAILURE`) within 4 lines
  after a block is absorbed into it; a bare "failed" line counts only if it is the very
  last output line of an execution and nothing else was found.
* Lines at `INFO`/`DEBUG`/`WARN` level and negated phrases (`0 errors`, `failed=0`)
  are ignored by single-line rules.
* Distinct logged occurrences (e.g. `ERROR: attempt 1 failed`, `ERROR: attempt 2 failed`)
  are counted separately.

Add a rule:

```python
from cperr.detection import DEFAULT_RULES, detect_errors, line_rule
rules = (line_rule("mytool", r"^MYTOOL-\d+ exploded"),) + DEFAULT_RULES
```

or append a `line_rule(...)` / `Rule(...)` entry to `DEFAULT_RULES`.

### Context and merging

Each selected block is the anchor: range = `[block.start - l, block.end + l]`, clamped to
the execution (context never spills into prompts or neighbouring executions). A long
traceback is **never** cut to fit `-l`. Ranges are sorted, merged when they overlap or
touch, and each source line is printed once in original order. Merging affects only
output ranges, never the count of selected errors.

## Limitations

* Heuristics, not a parser: unusual log formats may be missed (add a rule) and some
  prose with severity keywords may be over-counted. Use `--verbose` to see why a block was found.
* Multi-line errors whose continuation lines are not indented/recognised (e.g. free-form
  messages after an `ERROR:` line) are counted as one occurrence at the log line plus
  whatever context you request.
* Two different log lines about the same root cause (e.g. `ERROR: x` then `FATAL: x`) are
  two occurrences by design.
* Terminal copy/paste may wrap long lines or lose colours; wrapped lines can't be re-joined.
  ANSI colour codes are ignored for detection but preserved in output.
* Prompts with unusual themes may need `--prompt-regex`. Multi-line prompts (e.g. powerlevel10k
  two-line prompts) are only recognised on the line that matches a pattern.
* macOS only for clipboard access; `--stdin` works anywhere.
