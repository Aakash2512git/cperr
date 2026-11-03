"""Command-line interface: argument parsing and orchestration."""

from __future__ import annotations

import argparse
import os
import sys
from typing import Callable, Sequence, TextIO

from . import __version__
from .clipboard import ClipboardError, read_clipboard, write_clipboard
from .detection import detect_errors
from .executions import compile_user_patterns, split_executions
from .extraction import build_extraction, select_errors
from .models import Analysis, Extraction
from .report import format_extraction, format_summary

EXIT_OK = 0
EXIT_NO_ERRORS = 1  # nothing to extract (or empty input)
EXIT_USAGE = 2
EXIT_CLIPBOARD = 3

DEFAULT_RECORDS = 1
DEFAULT_LINES = 10


def _int_arg(minimum: int) -> Callable[[str], int]:
    def parse(v: str) -> int:
        try:
            n = int(v)
        except ValueError:
            raise argparse.ArgumentTypeError(f"{v!r} is not an integer") from None
        if n < minimum:
            raise argparse.ArgumentTypeError(f"must be {minimum} or greater")
        return n

    return parse


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="cperr",
        description=(
            "Extract the most useful error(s) from copied terminal output. Reads the "
            "macOS clipboard (pbpaste), finds logical error occurrences, adds context "
            "lines, prints the result, and copies it back to the clipboard (pbcopy). "
            "Summaries go to stderr; extracted text goes to stdout."
        ),
        epilog=(
            "exit status: 0 ok; 1 no errors found / empty input; 2 bad arguments; "
            "3 clipboard failure.  Env: CPERR_PROMPT_REGEX (extra prompt regexes, one per line)."
        ),
    )
    p.add_argument("-r", "--records", type=_int_arg(1), default=None, metavar="N",
                   help=f"number of latest error occurrences to select (default: {DEFAULT_RECORDS})")
    p.add_argument("-l", "--lines", type=_int_arg(0), default=None, metavar="N",
                   help=f"context lines above and below each error (default: {DEFAULT_LINES})")
    p.add_argument("--run", type=_int_arg(1), default=None, metavar="N",
                   help="only use the Nth execution counting back from the latest "
                        "(1 = latest, 2 = the one before, ...)")
    p.add_argument("--all", action="store_true", dest="all_errors",
                   help="extract all detected errors (from all executions, or from --run)")
    p.add_argument("--count", action="store_true",
                   help="only report error counts per execution; never touches the clipboard")
    p.add_argument("--stdin", action="store_true",
                   help="read input from standard input instead of the clipboard")
    p.add_argument("--dry-run", action="store_true",
                   help="print the extracted output but do not modify the clipboard")
    p.add_argument("-n", "--line-numbers", action="store_true",
                   help="prefix extracted lines with their line number in the input")
    p.add_argument("--prompt-regex", action="append", default=[], metavar="REGEX",
                   help="additional regex that identifies a shell prompt line "
                        "(repeatable; optional named group 'cmd' captures the command)")
    p.add_argument("-v", "--verbose", action="store_true",
                   help="print diagnostics about detection and extraction to stderr")
    p.add_argument("-q", "--quiet", action="store_true",
                   help="suppress the summary on stderr (errors are still shown)")
    p.add_argument("--version", action="version", version=f"cperr {__version__}")
    return p


def analyze(text: str, prompt_regexes: Sequence[str] = ()) -> Analysis:
    split = split_executions(text, extra_patterns=compile_user_patterns(prompt_regexes))
    errors = tuple(tuple(detect_errors(ex.scan_lines)) for ex in split.executions)
    return Analysis(split, errors)


def _verbose_report(analysis: Analysis, extraction: Extraction | None, err: TextIO) -> None:
    sp = analysis.split
    print("[verbose] prompt boundaries: "
          + (f"found at input lines {list(sp.prompt_lines)}" if sp.boundaries_known
             else "none recognised"), file=err)
    for pos, ex in enumerate(analysis.executions):
        cmd = f" command={ex.command!r}" if ex.command else ""
        print(f"[verbose] execution {pos + 1}: input lines {ex.line_no(0)}-"
              f"{ex.line_no(len(ex.lines) - 1)}{cmd}"
              f"{' (no prompt before it)' if ex.partial_start else ''}", file=err)
        for b in analysis.errors[pos]:
            print(f"[verbose]   error rule={b.rule} lines {ex.line_no(b.start)}-"
                  f"{ex.line_no(b.end)}: {b.headline}", file=err)
    if extraction:
        for s in extraction.sections:
            ex = analysis.executions[s.execution_position]
            rs = ", ".join(f"{ex.line_no(r.start)}-{ex.line_no(r.end)}" for r in s.ranges)
            print(f"[verbose] execution {s.execution_position + 1}: {len(s.selected)} selected "
                  f"-> output ranges (input lines) {rs}", file=err)


def main(
    argv: Sequence[str] | None = None,
    *,
    stdin: TextIO | None = None,
    stdout: TextIO | None = None,
    stderr: TextIO | None = None,
    read_clip: Callable[[], str] | None = None,
    write_clip: Callable[[str], None] | None = None,
) -> int:
    """Run the CLI. All I/O is injectable for testing."""
    stdin = stdin or sys.stdin
    out = stdout or sys.stdout
    err = stderr or sys.stderr
    read_clip = read_clip or read_clipboard
    write_clip = write_clip or write_clipboard

    parser = build_parser()
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # argparse already printed the message / help
        return int(exc.code or 0)

    def fail_usage(msg: str) -> int:
        print(f"cperr: error: {msg}", file=err)
        return EXIT_USAGE

    if args.all_errors and args.records is not None:
        return fail_usage("--all and -r/--records cannot be used together")

    records = args.records if args.records is not None else DEFAULT_RECORDS
    context = args.lines if args.lines is not None else DEFAULT_LINES

    # ---- read input ---------------------------------------------------------
    try:
        text = stdin.read() if args.stdin else read_clip()
    except ClipboardError as exc:
        print(f"cperr: clipboard error: {exc}", file=err)
        return EXIT_CLIPBOARD
    except UnicodeDecodeError as exc:
        return fail_usage(f"input is not valid UTF-8: {exc}")

    if not text.strip():
        src = "standard input" if args.stdin else "clipboard"
        print(f"cperr: {src} is empty - nothing to analyse (clipboard unchanged).", file=err)
        return EXIT_NO_ERRORS

    # ---- analyse --------------------------------------------------------------
    prompt_regexes = [r for r in os.environ.get("CPERR_PROMPT_REGEX", "").splitlines() if r.strip()]
    prompt_regexes += args.prompt_regex
    try:
        analysis = analyze(text, prompt_regexes)
    except ValueError as exc:
        return fail_usage(str(exc))

    if not analysis.executions:
        print("cperr: no command output found in the input (clipboard unchanged).", file=err)
        return EXIT_NO_ERRORS

    if args.run is not None:
        try:
            analysis.run_to_index(args.run)
        except ValueError as exc:
            return fail_usage(str(exc))

    # ---- --count: report only; stdout stays empty, clipboard untouched ---------
    if args.count:
        if not args.quiet:
            print(format_summary(analysis, run=args.run), file=err)
        if args.verbose:
            _verbose_report(analysis, None, err)
        return EXIT_OK

    # ---- select + extract -----------------------------------------------------
    selected = select_errors(analysis, count=records, run=args.run, select_all=args.all_errors)

    if not selected:
        if not args.quiet:
            print(format_summary(analysis, run=args.run), file=err)
        scope = f"in execution --run {args.run}" if args.run is not None else "in the input"
        print(f"\ncperr: no errors detected {scope}. Clipboard left unchanged.", file=err)
        if args.verbose:
            _verbose_report(analysis, None, err)
        return EXIT_NO_ERRORS

    extraction = build_extraction(analysis, selected, context)
    result = format_extraction(analysis, extraction, line_numbers=args.line_numbers)

    if not args.quiet:
        print(format_summary(analysis, extraction, run=args.run), file=err)
        print(file=err)
    if args.verbose:
        _verbose_report(analysis, extraction, err)

    print(result, file=out)

    if args.dry_run:
        print("cperr: dry run - clipboard not modified.", file=err)
        return EXIT_OK

    try:
        write_clip(result + "\n")
    except ClipboardError as exc:
        print(f"cperr: could not copy to clipboard: {exc}", file=err)
        return EXIT_CLIPBOARD
    if not args.quiet:
        print("cperr: copied to clipboard.", file=err)
    return EXIT_OK
