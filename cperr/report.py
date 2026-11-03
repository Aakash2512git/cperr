"""Formatting: summary text (stderr) and extracted text (stdout)."""

from __future__ import annotations

from .models import Analysis, Extraction


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def _label(analysis: Analysis, pos: int) -> str:
    """Execution label with oldest/latest markers and its --run number."""
    n = len(analysis.executions)
    tags = []
    if pos == 0:
        tags.append("oldest")
    if pos == n - 1:
        tags.append("latest")
    tags.append(f"--run {analysis.run_number(pos)}")
    return f"Execution {pos + 1} ({', '.join(tags)})"


def format_summary(
    analysis: Analysis,
    extraction: Extraction | None = None,
    *,
    run: int | None = None,
) -> str:
    out: list[str] = []
    n = len(analysis.executions)
    split = analysis.split

    if n == 0:
        return "Executions detected: 0 (no command output found)"

    out.append(f"Executions detected: {n}")
    if not split.boundaries_known:
        out.append(
            "  Note: no shell prompt recognised - execution boundaries are UNKNOWN; "
            "all text is treated as one (possibly incomplete) execution."
        )
    else:
        if analysis.executions[0].partial_start:
            out.append(
                "  Note: the oldest execution has no prompt line (its beginning was "
                "probably not copied), so it may be incomplete."
            )
        out.append(
            "  Note: only the copied text is analysed; earlier shell history is not included."
        )
    out.append("")

    width = max(len(_label(analysis, p)) for p in range(n))
    for pos in range(n):
        count = len(analysis.errors[pos])
        mark = ""
        if run is not None and analysis.run_to_index(run) == pos:
            mark = "  <- analysed (--run)"
        out.append(f"{_label(analysis, pos):<{width}} : {plural(count, 'error')}{mark}")
    out.append("")
    out.append(f"Total detected errors: {analysis.total_errors}")

    if extraction is not None:
        out.append(f"Selected error occurrences: {extraction.selected_count}")
        out.append(f"Context lines: {extraction.context} above and below")
        out.append(
            f"Output sections: {extraction.range_count} "
            "(overlapping or adjacent ranges: merged)"
        )
    return "\n".join(out)


def format_extraction(
    analysis: Analysis,
    extraction: Extraction,
    *,
    line_numbers: bool = False,
    separator: str = "... [lines omitted] ...",
) -> str:
    """Return the text for stdout/clipboard.

    Source lines are the original lines (ANSI codes untouched). An execution
    header is emitted only when more than one execution is involved.
    """
    multi = len(extraction.sections) > 1
    out: list[str] = []

    for si, section in enumerate(extraction.sections):
        ex = analysis.executions[section.execution_position]
        if multi:
            if si:
                out.append("")
            tags = []
            if section.execution_position == 0:
                tags.append("oldest")
            if section.execution_position == len(analysis.executions) - 1:
                tags.append("latest")
            tag = f", {', '.join(tags)}" if tags else ""
            cmd = f" - {ex.command}" if ex.command else ""
            out.append(
                f"=== Execution {ex.index} of {len(analysis.executions)} "
                f"(--run {analysis.run_number(section.execution_position)}{tag}){cmd} ==="
            )
        num_w = len(str(ex.line_no(len(ex.lines) - 1)))
        for ri, rng in enumerate(section.ranges):
            if ri:
                out.append(separator)
            for i in range(rng.start, rng.end + 1):
                line = ex.lines[i]
                out.append(f"{ex.line_no(i):>{num_w}}: {line}" if line_numbers else line)
    return "\n".join(out)
