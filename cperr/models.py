"""Plain data model shared by all cperr modules.

Everything here is immutable and free of behaviour beyond trivial helpers, so
the three pipeline stages (execution splitting, error detection, context
extraction) only communicate through these types.

Line indices inside an :class:`Execution` are 0-based and *inclusive* on both
ends when used in ``ErrorBlock`` / ``LineRange``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorBlock:
    """One logical error occurrence (e.g. a whole traceback) in an execution."""

    start: int  # first line of the block (inclusive, execution-relative)
    end: int  # last line of the block (inclusive, execution-relative)
    rule: str  # name of the detection rule that produced it
    headline: str  # first line, trimmed; used for diagnostics

    @property
    def line_count(self) -> int:
        return self.end - self.start + 1


@dataclass(frozen=True)
class Execution:
    """The captured output of one command run.

    ``index`` is 1-based and counts from the *oldest* execution in the input.
    ``lines`` holds the original text; ``scan_lines`` is the same text with
    ANSI escapes removed and is what detection runs on.
    """

    index: int
    lines: tuple[str, ...]
    scan_lines: tuple[str, ...]
    first_line_no: int  # 1-based line number of lines[0] in the whole input
    prompt: str | None = None  # the prompt line that started this execution
    command: str | None = None  # command text typed after the prompt, if any
    partial_start: bool = False  # output began before any recognised prompt

    def line_no(self, i: int) -> int:
        """Line number in the whole input for execution-relative index ``i``."""
        return self.first_line_no + i


@dataclass(frozen=True)
class SplitResult:
    executions: tuple[Execution, ...]
    boundaries_known: bool  # False when no shell prompt was recognised
    prompt_lines: tuple[int, ...] = ()  # 1-based input line numbers of prompts


@dataclass(frozen=True)
class Analysis:
    """Executions plus the error blocks detected in each (parallel tuples)."""

    split: SplitResult
    errors: tuple[tuple[ErrorBlock, ...], ...]

    @property
    def executions(self) -> tuple[Execution, ...]:
        return self.split.executions

    @property
    def total_errors(self) -> int:
        return sum(len(e) for e in self.errors)

    def run_to_index(self, run: int) -> int:
        """Translate a 1-based ``--run`` number (1 = latest) to a 0-based
        position in ``executions``. Raises ``ValueError`` when out of range."""
        n = len(self.executions)
        if not 1 <= run <= n:
            raise ValueError(
                f"--run {run} is out of range: {n} execution{'s' if n != 1 else ''} "
                f"detected (valid values: 1-{n})"
            )
        return n - run

    def run_number(self, position: int) -> int:
        """Inverse of :meth:`run_to_index` (1 = latest)."""
        return len(self.executions) - position


@dataclass(frozen=True)
class SelectedError:
    execution_position: int  # 0-based position in Analysis.executions
    block: ErrorBlock


@dataclass(frozen=True)
class LineRange:
    start: int  # inclusive, execution-relative
    end: int  # inclusive, execution-relative


@dataclass(frozen=True)
class Section:
    """Merged context ranges for one execution."""

    execution_position: int
    ranges: tuple[LineRange, ...]
    selected: tuple[ErrorBlock, ...]


@dataclass(frozen=True)
class Extraction:
    selected: tuple[SelectedError, ...]  # chronological order
    sections: tuple[Section, ...]  # chronological order
    context: int

    @property
    def selected_count(self) -> int:
        return len(self.selected)

    @property
    def range_count(self) -> int:
        return sum(len(s.ranges) for s in self.sections)
