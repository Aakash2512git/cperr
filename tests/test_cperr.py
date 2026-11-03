"""Unit tests for cperr. No real clipboard is used; pbcopy/pbpaste are mocked."""

import io
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cperr import clipboard
from cperr.cli import analyze, main
from cperr.clipboard import ClipboardError
from cperr.detection import DEFAULT_RULES, detect_errors, line_rule
from cperr.executions import match_prompt, split_executions
from cperr.extraction import build_extraction, merge_ranges, select_errors
from cperr.models import LineRange
from cperr.report import format_extraction

P = "akashshukla@Akashs-MacBook-Pro scan-reports %"


def shell(*runs, trailing_prompt=True):
    """Build terminal text: each run is (command, [output lines])."""
    out = []
    for cmd, lines in runs:
        out.append(f"{P} {cmd}")
        out.extend(lines)
        out.append("")
    if trailing_prompt:
        out.append(P)
    return "\n".join(out) + "\n"


def numbered(n, prefix="line"):
    return [f"{prefix} {i}" for i in range(n)]


class Runner:
    """Run the CLI with injected I/O and a fake clipboard."""

    def __init__(self, clip="", stdin=""):
        self.clip = clip
        self.writes = []
        self.out = io.StringIO()
        self.err = io.StringIO()
        self.stdin = io.StringIO(stdin)

    def _write(self, text):
        self.writes.append(text)

    def run(self, *argv):
        with mock.patch("sys.stderr", io.StringIO()):  # argparse writes here
            self.code = main(
                list(argv),
                stdin=self.stdin,
                stdout=self.out,
                stderr=self.err,
                read_clip=lambda: self.clip,
                write_clip=self._write,
            )
        return self


def counts(text):
    a = analyze(text)
    return [len(e) for e in a.errors]


class TestDetection(unittest.TestCase):
    def test_01_single_standalone_error(self):
        blocks = detect_errors(["INFO: start", "ERROR: Connection refused", "INFO: done"])
        self.assertEqual([(b.start, b.end) for b in blocks], [(1, 1)])

    def test_02_python_traceback_is_one_occurrence(self):
        lines = [
            "starting",
            "Traceback (most recent call last):",
            '  File "a.py", line 3, in <module>',
            "    main()",
            '  File "a.py", line 1, in main',
            "    raise ValueError('boom')",
            "ValueError: boom",
            "done",
        ]
        blocks = detect_errors(lines)
        self.assertEqual([(b.start, b.end) for b in blocks], [(1, 6)])
        self.assertEqual(blocks[0].rule, "python-traceback")

    def test_02b_chained_python_traceback_is_one_occurrence(self):
        lines = [
            "Traceback (most recent call last):",
            '  File "a.py", line 3, in f',
            "KeyError: 'x'",
            "",
            "During handling of the above exception, another exception occurred:",
            "",
            "Traceback (most recent call last):",
            '  File "a.py", line 5, in g',
            "RuntimeError: wrapped",
        ]
        self.assertEqual([(b.start, b.end) for b in detect_errors(lines)], [(0, 8)])

    def test_03_java_nested_caused_by_is_one_occurrence(self):
        lines = [
            "INFO starting",
            'Exception in thread "main" java.lang.RuntimeException: failed to start',
            "\tat com.acme.App.main(App.java:10)",
            "Caused by: java.io.IOException: disk error",
            "\tat com.acme.Io.read(Io.java:5)",
            "\t... 1 more",
            "Caused by: java.net.SocketException: reset",
            "\tat java.net.Socket.read(Socket.java:1)",
            "\t... 2 more",
            "INFO exiting",
        ]
        blocks = detect_errors(lines)
        self.assertEqual([(b.start, b.end) for b in blocks], [(1, 8)])

    def test_node_error_with_frames(self):
        lines = [
            "TypeError: Cannot read properties of undefined (reading 'x')",
            "    at foo (/app/index.js:3:9)",
            "    at Object.<anonymous> (/app/index.js:9:1)",
            "",
            "Node.js v20.1.0",
        ]
        blocks = detect_errors(lines)
        self.assertEqual(len(blocks), 1)
        self.assertEqual(blocks[0].start, 0)
        self.assertEqual(blocks[0].end, 2)

    def test_go_panic(self):
        lines = [
            "panic: runtime error: index out of range [3] with length 3",
            "",
            "goroutine 1 [running]:",
            "main.main()",
            "\t/app/main.go:10 +0x1d",
            "exit status 2",
        ]
        blocks = detect_errors(lines)
        self.assertEqual([(b.start, b.end) for b in blocks], [(0, 5)])

    def test_shell_failures(self):
        self.assertEqual(len(detect_errors(["zsh: command not found: foo"])), 1)
        self.assertEqual(len(detect_errors(["cat: /etc/shadow: Permission denied"])), 1)
        self.assertEqual(len(detect_errors(["curl: (7) Failed to connect to localhost port 80"])), 1)

    def test_docker_k8s_http_scanner(self):
        for line in (
            "docker: Error response from daemon: pull access denied for foo",
            "pod/app-1  0/1  CrashLoopBackOff  4  2m",
            "HTTP/1.1 502 Bad Gateway",
            "Total: 5 (HIGH: 3, CRITICAL: 2)",
        ):
            self.assertEqual(len(detect_errors([line])), 1, line)

    def test_log_line_followed_by_stack_trace_is_one_occurrence(self):
        lines = [
            "2024-01-01 12:00:00 ERROR Request failed",
            "java.lang.NullPointerException: x",
            "\tat a.B.c(B.java:1)",
            "2024-01-01 12:00:01 INFO next",
        ]
        self.assertEqual([(b.start, b.end) for b in detect_errors(lines)], [(0, 2)])

    def test_distinct_retry_failures_counted_separately(self):
        lines = ["ERROR: attempt 1 failed", "retrying", "ERROR: attempt 2 failed", "retrying",
                 "ERROR: attempt 3 failed"]
        self.assertEqual(len(detect_errors(lines)), 3)

    def test_informational_failed_not_counted(self):
        lines = [
            "INFO: 0 tests failed",
            "INFO: retrying failed uploads later",
            "Summary: 0 errors, 12 passed",
            "WARNING: previous run failed to start, skipping",
            "all good, nothing failed=0",
            "The word error alone in prose",
        ]
        self.assertEqual(detect_errors(lines), [])

    def test_summary_line_after_error_is_absorbed(self):
        lines = ["ERROR: boom", "Scan failed"]
        blocks = detect_errors(lines)
        self.assertEqual([(b.start, b.end) for b in blocks], [(0, 1)])

    def test_lone_failure_word_only_as_last_line_fallback(self):
        self.assertEqual(len(detect_errors(["working", "Scan failed"])), 1)
        self.assertEqual(detect_errors(["Scan failed", "something else", "more"]), [])

    def test_realistic_noise_is_not_counted(self):
        for lines in (
            ["total 8", "drwxr-xr-x  errors_dir", "-rw-r--r-- error.log"],
            ["PASS a.test.js", "Tests: 0 failed, 5 passed"],
            ["INFO: request timed out, retrying", "DEBUG connection refused earlier"],
            ["[INFO] BUILD SUCCESS"],
            ["Handling errors gracefully is important"],
        ):
            self.assertEqual(detect_errors(lines), [], lines)

    def test_runs_of_related_lines_are_one_occurrence(self):
        mvn = ["[ERROR] Failed to execute goal x", "[ERROR] -> [Help 1]", "[INFO] BUILD FAILURE"]
        self.assertEqual([(b.start, b.end) for b in detect_errors(mvn)], [(0, 2)])
        pytest_out = ["    def t():", ">   assert 1 == 2", "E   assert 1 == 2", "E    + where 1 = f()",
                      "=== 1 failed in 0.1s ==="]
        self.assertEqual([(b.start, b.end) for b in detect_errors(pytest_out)], [(2, 4)])
        npm = ["npm ERR! code ELIFECYCLE", "npm ERR! errno 1", "npm ERR! a@1 test: `jest`"]
        self.assertEqual([(b.start, b.end) for b in detect_errors(npm)], [(0, 2)])

    def test_custom_rule_extensibility(self):
        rules = (line_rule("mine", r"^MYTOOL-\d+ exploded"),) + DEFAULT_RULES
        self.assertEqual(len(detect_errors(["MYTOOL-42 exploded", "ok"], rules)), 1)
        self.assertEqual(detect_errors(["MYTOOL-42 exploded", "ok"]), [])

    def test_ansi_is_ignored_for_detection_but_preserved_in_output(self):
        text = shell(("run", ["\x1b[31mERROR: red\x1b[0m"]))
        a = analyze(text)
        self.assertEqual(counts(text), [1])
        ex = build_extraction(a, select_errors(a), 0)
        self.assertIn("\x1b[31m", format_extraction(a, ex))


class TestExecutions(unittest.TestCase):
    def test_06_multiple_executions_with_different_error_counts(self):
        text = shell(
            ("./scan.sh", ["INFO: Starting", "ERROR: a", "Traceback (most recent call last):",
                           '  File "x.py", line 1, in <module>', "ValueError: v",
                           "ERROR: b", "Scan failed"]),
            ("./scan.sh", ["INFO: Starting", "ERROR: Timeout", "Scan failed"]),
            ("ls", ["a.txt", "b.txt"]),
        )
        a = analyze(text)
        self.assertEqual(len(a.executions), 3)
        self.assertEqual([len(e) for e in a.errors], [2, 1, 0])
        self.assertEqual(a.total_errors, 3)
        self.assertTrue(a.split.boundaries_known)

    def test_07_trailing_empty_prompt_creates_no_execution(self):
        text = shell(("./scan.sh", ["ERROR: x"]))
        self.assertEqual(len(analyze(text).executions), 1)
        self.assertEqual(len(split_executions(P + "\n").executions), 0)
        # also several consecutive empty prompts
        self.assertEqual(len(split_executions(f"{P}\n{P}\n{P}\n").executions), 0)

    def test_command_text_captured(self):
        sp = split_executions(shell(("./scan.sh --fast", ["out"])))
        self.assertEqual(sp.executions[0].command, "./scan.sh --fast")

    def test_14_no_prompts_means_unknown_boundaries(self):
        text = "INFO a\nERROR: boom\nINFO b\n"
        a = analyze(text)
        self.assertFalse(a.split.boundaries_known)
        self.assertEqual(len(a.executions), 1)
        self.assertEqual(a.total_errors, 1)
        r = Runner(clip=text).run("--dry-run")
        self.assertIn("UNKNOWN", r.err.getvalue())

    def test_text_before_first_prompt_is_flagged_partial(self):
        text = "ERROR: early\n" + shell(("ls", ["ok"]))
        a = analyze(text)
        self.assertEqual(len(a.executions), 2)
        self.assertTrue(a.executions[0].partial_start)
        self.assertFalse(a.executions[1].partial_start)

    def test_18_prompt_like_text_is_not_a_prompt(self):
        not_prompts = [
            "Progress: 50% done",
            "100%",
            "echo $HOME",
            "  indented % thing",
            "john@example.com is 50% done",
            "user@host: connection lost > retry",
            "cost is 5 $ per unit",
            "> quoted output",
            "ERROR: path /tmp/x % failed",
            "usage: tool [options] % file",
            "ratio 3 % 2",
            "$ literal dollar line",
        ]
        for line in not_prompts:
            self.assertIsNone(match_prompt(line), line)
        text = shell(("./job.sh", ["Progress: 10%", "cost is 5 $ per unit", "> quoted",
                                   "user@host: x > y", "ERROR: failed"]))
        a = analyze(text)
        self.assertEqual(len(a.executions), 1)
        self.assertEqual(len(a.executions[0].lines), 5)

    def test_common_prompt_formats_recognised(self):
        prompts = [
            "alice@host proj % ls",
            "alice@host ~ % ",
            "alice@host % ",
            "(venv) alice@host proj % pytest",
            "bob@srv:~/code$ make",
            "bob@srv:/var/log# tail x",
            "[root@srv log]# ls",
            "~/code/proj % git status",
            "➜  proj git:(main) ✗ ls",
            "❯ npm test",
        ]
        for pr in prompts:
            self.assertIsNotNone(match_prompt(pr), pr)

    def test_no_hardcoded_username(self):
        text = "carol@laptop-9 api % make\nERROR: x\n\ncarol@laptop-9 api %\n"
        a = analyze(text)
        self.assertEqual((len(a.executions), a.total_errors), (1, 1))

    def test_custom_prompt_regex(self):
        text = "<<job1>> run\nERROR: a\n<<job2>> run\nERROR: b\nERROR: c\n"
        self.assertEqual(len(analyze(text).executions), 1)
        a = analyze(text, [r"^<<job\d+>>\s+(?P<cmd>.*)$"])
        self.assertEqual([len(e) for e in a.errors], [1, 2])

    def test_crlf_input(self):
        text = shell(("x", ["ERROR: a"])).replace("\n", "\r\n")
        self.assertEqual(analyze(text).total_errors, 1)


class TestSelectionAndContext(unittest.TestCase):
    def setUp(self):
        self.errs = [f"ERROR: e{i}" for i in range(5)]

    def _analysis(self, gap):
        lines = []
        for e in self.errs:
            lines.append(e)
            lines += numbered(gap, "ctx")
        return analyze(shell(("run", lines)))

    def test_04_overlapping_contexts_merge_without_duplicates(self):
        a = self._analysis(2)
        sel = select_errors(a, count=2)
        ex = build_extraction(a, sel, 5)
        self.assertEqual(ex.selected_count, 2)
        self.assertEqual(ex.range_count, 1)
        out = format_extraction(a, ex).splitlines()
        src = a.executions[0].lines
        r = ex.sections[0].ranges[0]
        self.assertEqual(out, list(src[r.start:r.end + 1]))

    def test_05_distant_errors_produce_separate_sections(self):
        a = self._analysis(30)
        sel = select_errors(a, count=2)
        ex = build_extraction(a, sel, 3)
        self.assertEqual(ex.selected_count, 2)
        self.assertEqual(ex.range_count, 2)
        out = format_extraction(a, ex)
        self.assertEqual(out.count("... [lines omitted] ..."), 1)

    def test_merge_never_collapses_error_count(self):
        a = self._analysis(0)  # 5 adjacent errors
        ex = build_extraction(a, select_errors(a, count=5), 10)
        self.assertEqual(ex.selected_count, 5)
        self.assertEqual(ex.range_count, 1)

    def test_touching_ranges_merge(self):
        self.assertEqual(
            merge_ranges([LineRange(0, 3), LineRange(4, 6), LineRange(8, 9)]),
            [LineRange(0, 6), LineRange(8, 9)],
        )
        self.assertEqual(merge_ranges([LineRange(5, 9), LineRange(0, 6)]), [LineRange(0, 9)])

    def test_08_bottom_up_selection_keeps_chronological_order(self):
        a = self._analysis(1)
        sel = select_errors(a, count=2)
        self.assertEqual([s.block.headline for s in sel], ["ERROR: e3", "ERROR: e4"])
        r = Runner(clip=shell(("run", [l for e in self.errs for l in (e, "ok")]))).run(
            "-r", "2", "-l", "0", "--dry-run")
        self.assertEqual([l for l in r.out.getvalue().splitlines() if l.startswith("ERROR")],
                         ["ERROR: e3", "ERROR: e4"])
        self.assertIn("e3", r.out.getvalue())
        self.assertNotIn("e2", r.out.getvalue())
        self.assertLess(r.out.getvalue().index("e3"), r.out.getvalue().index("e4"))

    def test_09_fewer_errors_than_requested_returns_all(self):
        a = self._analysis(1)
        self.assertEqual(len(select_errors(a, count=50)), 5)

    def test_10_no_errors(self):
        text = shell(("ls", ["a", "b", "c"]))
        r = Runner(clip=text).run()
        self.assertEqual(r.code, 1)
        self.assertEqual(r.writes, [])
        self.assertEqual(r.out.getvalue(), "")
        self.assertIn("no errors detected", r.err.getvalue())

    def test_12_context_clamped_at_start_and_end(self):
        lines = ["ERROR: first", "a", "b", "c", "ERROR: last"]
        a = analyze(shell(("run", lines)))
        ex = build_extraction(a, select_errors(a, count=5), 100)
        r = ex.sections[0].ranges[0]
        self.assertEqual((r.start, r.end), (0, 4))
        # context must not leak into the prompt line or neighbouring executions
        out = format_extraction(a, ex)
        self.assertNotIn("%", out)

    def test_context_does_not_cross_execution_boundary(self):
        text = shell(("one", ["junk1", "junk2", "ok"]), ("two", ["ERROR: here", "tail"]))
        r = Runner(clip=text).run("-l", "50", "--dry-run")
        self.assertNotIn("junk", r.out.getvalue())
        self.assertNotIn("%", r.out.getvalue())

    def test_13_multiline_block_exceeding_context_is_never_truncated(self):
        frames = [f'  File "m{i}.py", line {i}, in f{i}' for i in range(40)]
        tb = ["Traceback (most recent call last):"] + frames + ["ValueError: deep"]
        lines = ["pre1", "pre2"] + tb + ["post1", "post2"]
        a = analyze(shell(("run", lines)))
        self.assertEqual(a.total_errors, 1)
        ex = build_extraction(a, select_errors(a), 1)
        out = format_extraction(a, ex).splitlines()
        self.assertEqual(out, ["pre2"] + tb + ["post1"])
        ex0 = build_extraction(a, select_errors(a), 0)
        self.assertEqual(format_extraction(a, ex0).splitlines(), tb)

    def test_15_run_selection(self):
        text = shell(
            ("a", ["ERROR: old1", "ERROR: old2"]),
            ("b", ["ERROR: mid"]),
            ("c", ["ERROR: new"]),
        )
        a = analyze(text)
        self.assertEqual([s.block.headline for s in select_errors(a, run=1)], ["ERROR: new"])
        self.assertEqual([s.block.headline for s in select_errors(a, run=2)], ["ERROR: mid"])
        self.assertEqual([s.block.headline for s in select_errors(a, run=3, count=2)],
                         ["ERROR: old1", "ERROR: old2"])
        self.assertEqual([s.block.headline for s in select_errors(a, run=3, count=1)], ["ERROR: old2"])
        self.assertEqual(len(select_errors(a, select_all=True)), 4)
        self.assertEqual(len(select_errors(a, run=3, select_all=True)), 2)

    def test_15b_invalid_run_numbers(self):
        text = shell(("a", ["ERROR: x"]), ("b", ["ERROR: y"]))
        for bad in ("0", "3", "-1", "abc"):
            r = Runner(clip=text).run("--run", bad)
            self.assertEqual(r.code, 2, bad)
            self.assertEqual(r.writes, [], bad)
        r = Runner(clip=text).run("--run", "5")
        self.assertIn("out of range", r.err.getvalue())
        self.assertIn("1-2", r.err.getvalue())

    def test_run_with_r_and_l(self):
        text = shell(
            ("a", ["x1", "ERROR: a1", "x2", "ERROR: a2", "x3", "ERROR: a3", "x4"]),
            ("b", ["ERROR: b1"]),
        )
        r = Runner(clip=text).run("--run", "2", "-r", "3", "-l", "0", "--dry-run")
        self.assertEqual(r.out.getvalue().splitlines().count("... [lines omitted] ..."), 2)
        self.assertEqual(r.out.getvalue().count("ERROR:"), 3)
        self.assertNotIn("b1", r.out.getvalue())


class TestCli(unittest.TestCase):
    TEXT = shell(("./scan.sh", ["INFO: s", "ERROR: Connection refused", "Scan failed"]),
                 ("./scan.sh", ["INFO: s", "ERROR: Timeout", "Scan failed"]))

    def test_default_extraction_prints_and_copies(self):
        r = Runner(clip=self.TEXT).run()
        self.assertEqual(r.code, 0)
        self.assertIn("ERROR: Timeout", r.out.getvalue())
        self.assertNotIn("Connection refused", r.out.getvalue())
        self.assertEqual(len(r.writes), 1)
        self.assertEqual(r.writes[0], r.out.getvalue())
        # summaries go to stderr only
        self.assertIn("Executions detected: 2", r.err.getvalue())
        self.assertNotIn("Executions detected", r.out.getvalue())
        self.assertIn("Total detected errors: 2", r.err.getvalue())
        self.assertIn("Selected error occurrences: 1", r.err.getvalue())
        self.assertIn("Context lines: 10 above and below", r.err.getvalue())
        self.assertIn("(oldest", r.err.getvalue())
        self.assertIn("(latest", r.err.getvalue())

    def test_all_flag_labels_executions(self):
        r = Runner(clip=self.TEXT).run("--all", "--dry-run")
        out = r.out.getvalue()
        self.assertIn("=== Execution 1 of 2 (--run 2, oldest)", out)
        self.assertIn("=== Execution 2 of 2 (--run 1, latest)", out)
        self.assertIn("Connection refused", out)
        self.assertIn("Timeout", out)

    def test_all_conflicts_with_records(self):
        self.assertEqual(Runner(clip=self.TEXT).run("--all", "-r", "2").code, 2)

    def test_invalid_numeric_arguments(self):
        for argv in (["-r", "0"], ["-r", "x"], ["-l", "-1"], ["-l", "abc"]):
            r = Runner(clip=self.TEXT).run(*argv)
            self.assertEqual(r.code, 2, argv)
            self.assertEqual(r.writes, [])

    def test_16_count_does_not_touch_clipboard_or_stdout(self):
        r = Runner(clip=self.TEXT).run("--count")
        self.assertEqual(r.code, 0)
        self.assertEqual(r.writes, [])
        self.assertEqual(r.out.getvalue(), "")
        self.assertIn("Total detected errors: 2", r.err.getvalue())
        self.assertNotIn("Selected error occurrences", r.err.getvalue())

    def test_16_dry_run_does_not_touch_clipboard(self):
        r = Runner(clip=self.TEXT).run("--dry-run")
        self.assertEqual(r.code, 0)
        self.assertEqual(r.writes, [])
        self.assertIn("ERROR: Timeout", r.out.getvalue())

    def test_16_dry_run_with_mocked_pb_tools_never_calls_pbcopy(self):
        with mock.patch("cperr.clipboard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=self.TEXT.encode(), stderr=b"")
            out, err = io.StringIO(), io.StringIO()
            for flag in ("--dry-run", "--count"):
                main([flag], stdout=out, stderr=err)
            cmds = [c.args[0][0] for c in run.call_args_list]
            self.assertEqual(cmds, ["pbpaste", "pbpaste"])

    def test_11_empty_clipboard(self):
        for clip in ("", "   \n\n  "):
            r = Runner(clip=clip).run()
            self.assertEqual(r.code, 1)
            self.assertEqual(r.writes, [])
            self.assertIn("empty", r.err.getvalue())

    def test_only_prompts_in_clipboard(self):
        r = Runner(clip=P + "\n").run()
        self.assertEqual(r.code, 1)
        self.assertEqual(r.writes, [])

    def test_stdin_mode(self):
        r = Runner(clip="ERROR: from clipboard", stdin=self.TEXT).run("--stdin", "-r", "2", "-l", "1")
        self.assertEqual(r.code, 0)
        self.assertIn("Timeout", r.out.getvalue())
        self.assertNotIn("from clipboard", r.out.getvalue())

    def test_verbose_reports_rules(self):
        r = Runner(clip=self.TEXT).run("--verbose", "--dry-run")
        self.assertIn("[verbose]", r.err.getvalue())
        self.assertIn("rule=log-level", r.err.getvalue())
        r2 = Runner(clip=self.TEXT).run("--dry-run")
        self.assertNotIn("[verbose]", r2.err.getvalue())

    def test_line_numbers_option(self):
        r = Runner(clip="x\nERROR: boom\ny\n").run("-n", "-l", "1", "--dry-run")
        self.assertEqual(r.out.getvalue().splitlines(), ["1: x", "2: ERROR: boom", "3: y"])

    def test_invalid_prompt_regex(self):
        r = Runner(clip=self.TEXT).run("--prompt-regex", "([")
        self.assertEqual(r.code, 2)
        self.assertIn("invalid --prompt-regex", r.err.getvalue())

    def test_help_documents_all_options(self):
        buf = io.StringIO()
        with mock.patch("sys.stdout", buf):
            code = main(["--help"])
        self.assertEqual(code, 0)
        for opt in ("-r", "-l", "--run", "--all", "--count", "--stdin", "--dry-run", "--verbose"):
            self.assertIn(opt, buf.getvalue())

    def test_output_preserves_source_text_exactly(self):
        lines = ["  indented\tTab", "ERROR: x  trailing  ", "unicode ✓ é"]
        r = Runner(clip=shell(("run", lines))).run("-l", "5", "--dry-run")
        self.assertEqual(r.out.getvalue().splitlines(), lines)


class TestClipboard(unittest.TestCase):
    def test_17_missing_pbpaste(self):
        with mock.patch("cperr.clipboard.subprocess.run", side_effect=FileNotFoundError):
            r = Runner()
            code = main([], stdout=r.out, stderr=r.err)
        self.assertEqual(code, 3)
        self.assertIn("pbpaste", r.err.getvalue())
        self.assertIn("--stdin", r.err.getvalue())

    def test_17_pbpaste_nonzero_exit(self):
        cp = subprocess.CompletedProcess([], 1, stdout=b"", stderr=b"boom")
        with mock.patch("cperr.clipboard.subprocess.run", return_value=cp):
            r = Runner()
            code = main([], stdout=r.out, stderr=r.err)
        self.assertEqual(code, 3)
        self.assertIn("exited with status 1", r.err.getvalue())

    def test_17_pbcopy_failure_reported_after_printing(self):
        def fake(cmd, **kw):
            if cmd[0] == "pbpaste":
                return subprocess.CompletedProcess(cmd, 0, stdout=b"ERROR: x\n", stderr=b"")
            raise FileNotFoundError

        with mock.patch("cperr.clipboard.subprocess.run", side_effect=fake):
            r = Runner()
            code = main([], stdout=r.out, stderr=r.err)
        self.assertEqual(code, 3)
        self.assertIn("ERROR: x", r.out.getvalue())  # result still printed
        self.assertIn("could not copy to clipboard", r.err.getvalue())

    def test_17_clipboard_timeout(self):
        with mock.patch("cperr.clipboard.subprocess.run",
                        side_effect=subprocess.TimeoutExpired("pbpaste", 10)):
            with self.assertRaises(ClipboardError):
                clipboard.read_clipboard()

    def test_roundtrip_uses_utf8_bytes(self):
        with mock.patch("cperr.clipboard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout="é✓".encode(), stderr=b"")
            self.assertEqual(clipboard.read_clipboard(), "é✓")
            clipboard.write_clipboard("é✓")
            self.assertEqual(run.call_args.kwargs["input"], "é✓".encode())
            self.assertEqual(run.call_args.args[0], ["pbcopy"])

    def test_success_copies_exact_printed_text_plus_newline(self):
        with mock.patch("cperr.clipboard.subprocess.run") as run:
            run.return_value = subprocess.CompletedProcess([], 0, stdout=b"ERROR: x\n", stderr=b"")
            r = Runner()
            code = main([], stdout=r.out, stderr=r.err)
        self.assertEqual(code, 0)
        pbcopy_call = [c for c in run.call_args_list if c.args[0] == ["pbcopy"]][0]
        self.assertEqual(pbcopy_call.kwargs["input"], r.out.getvalue().encode())


if __name__ == "__main__":
    unittest.main()
