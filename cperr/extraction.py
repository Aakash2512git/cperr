"""Stage 3: choose which errors to keep and compute merged context ranges.

Knows about executions and error blocks, but not about how either was found.
"""

from __future__ import annotations

from typing import Optional

from .models import (
    Analysis,
    ErrorBlock,
    Extraction,
    LineRange,
    Section,
    SelectedError,
)


def select_errors(
    analysis: Analysis,
    *,
    count: int = 1,
    run: Optional[int] = None,
    select_all: bool = False,
) -> tuple[SelectedError, ...]:
    """Pick errors bottom-up (newest first); return them chronologically.

    ``run`` is 1-based, 1 = latest execution. Raises ``ValueError`` for an
    out-of-range run or a non-positive ``count``.
    """
    if not select_all and count < 1:
        raise ValueError("-r/--records must be at least 1")

    if run is not None:
        positions = [analysis.run_to_index(run)]
    else:
        positions = list(range(len(analysis.executions)))

    # Newest first: later executions first, later blocks first.
    newest_first: list[SelectedError] = []
    for pos in reversed(positions):
        for block in reversed(analysis.errors[pos]):
            newest_first.append(SelectedError(pos, block))

    chosen = newest_first if select_all else newest_first[:count]
    chosen.reverse()
    return tuple(chosen)


def context_range(block: ErrorBlock, context: int, n_lines: int) -> LineRange:
    """Block +/- ``context`` lines, clamped. The block itself is never cut."""
    return LineRange(max(0, block.start - context), min(n_lines - 1, block.end + context))


def merge_ranges(ranges: list[LineRange]) -> list[LineRange]:
    """Sort by start and merge ranges that overlap or touch (adjacent lines)."""
    merged: list[LineRange] = []
    for r in sorted(ranges, key=lambda r: (r.start, r.end)):
        if merged and r.start <= merged[-1].end + 1:
            if r.end > merged[-1].end:
                merged[-1] = LineRange(merged[-1].start, r.end)
        else:
            merged.append(r)
    return merged


def build_extraction(
    analysis: Analysis, selected: tuple[SelectedError, ...], context: int
) -> Extraction:
    if context < 0:
        raise ValueError("-l/--lines must be 0 or greater")

    by_pos: dict[int, list[ErrorBlock]] = {}
    for s in selected:
        by_pos.setdefault(s.execution_position, []).append(s.block)

    sections = []
    for pos in sorted(by_pos):
        n = len(analysis.executions[pos].lines)
        ranges = merge_ranges([context_range(b, context, n) for b in by_pos[pos]])
        sections.append(Section(pos, tuple(ranges), tuple(by_pos[pos])))
    return Extraction(selected, tuple(sections), context)
