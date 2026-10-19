"""Unit tests for log_scanner.py - run with:  python3 -m unittest discover -s python/tests -v"""

import io
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import log_scanner as ls  # noqa: E402

PY_DIR = Path(__file__).resolve().parents[1]
RULES = PY_DIR / "rules.toml"
SAMPLES = PY_DIR / "samples"


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def write(self, name: str, content: str) -> Path:
        path = self.tmp / name
        path.write_text(content, encoding="utf-8")
        return path


class ConfigTests(TempDirTestCase):
    def test_shipped_rules_are_valid(self):
        rules, threshold = ls.load_config(RULES)
        self.assertGreater(len(rules), 5)
        self.assertEqual(threshold, 3)

    def test_invalid_regex_is_rejected(self):
        cfg = self.write("bad.toml", '[[rules]]\nname="x"\npattern="("\nseverity="WARNING"\n')
        with self.assertRaises(ls.ConfigError):
            ls.load_config(cfg)

    def test_invalid_severity_is_rejected(self):
        cfg = self.write("bad.toml", '[[rules]]\nname="x"\npattern="a"\nseverity="PANIC"\n')
        with self.assertRaises(ls.ConfigError):
            ls.load_config(cfg)

    def test_duplicate_rule_name_is_rejected(self):
        rule = '[[rules]]\nname="x"\npattern="a"\nseverity="INFO"\n'
        cfg = self.write("bad.toml", rule + rule)
        with self.assertRaises(ls.ConfigError):
            ls.load_config(cfg)


class FileFilterTests(unittest.TestCase):
    def test_rule_with_file_filter(self):
        rule = ls.Rule("r", ls.re.compile("x"), "WARNING", ("*deploy*.log",))
        self.assertTrue(rule.applies_to("/var/log/release-app/deploy.log"))
        self.assertFalse(rule.applies_to("/var/log/nginx/release-app.error.log"))

    def test_rule_without_filter_applies_everywhere(self):
        rule = ls.Rule("r", ls.re.compile("x"), "WARNING")
        self.assertTrue(rule.applies_to("/any/file.log"))


class SampleLogTests(unittest.TestCase):
    """The shipped sample logs reproduce the incidents of this project."""

    def setUp(self):
        self.rules, self.threshold = ls.load_config(RULES)

    def scan(self, *names):
        return ls.scan([str(SAMPLES / n) for n in names], self.rules, {})

    def test_failed_deploy_raises_alert(self):
        result = self.scan("deploy.log")
        rules_hit = {m.rule for m in result.matches}
        self.assertIn("deploy-failed", rules_hit)
        self.assertEqual(result.count("WARNING"), 5)  # five failed health checks
        self.assertEqual(ls.evaluate(result, self.threshold), ls.EXIT_ALERT)

    def test_nginx_error_log(self):
        result = self.scan("release-app.error.log")
        self.assertEqual({m.rule for m in result.matches},
                         {"nginx-error", "nginx-crit", "nginx-permission-denied"})

    def test_access_log_detects_5xx_and_failed_health(self):
        result = self.scan("release-app.access.log")
        self.assertEqual(sorted(m.rule for m in result.matches),
                         ["health-endpoint-not-ok", "http-5xx"])

    def test_deploy_rules_ignore_other_files(self):
        result = self.scan("release-app.access.log")
        self.assertNotIn("deploy-failed", {m.rule for m in result.matches})


class EvaluationTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.rules = [ls.Rule("warn", ls.re.compile("WARN"), "WARNING"),
                      ls.Rule("crit", ls.re.compile("CRIT"), "CRITICAL")]

    def run_scan(self, content):
        path = self.write("app.log", content)
        return ls.scan([str(path)], self.rules, {})

    def test_clean_log_is_ok(self):
        self.assertEqual(ls.evaluate(self.run_scan("all good\n"), 3), ls.EXIT_OK)

    def test_warnings_below_threshold(self):
        self.assertEqual(ls.evaluate(self.run_scan("WARN\nWARN\n"), 3), ls.EXIT_WARNING)

    def test_warnings_reaching_threshold_alert(self):
        self.assertEqual(ls.evaluate(self.run_scan("WARN\n" * 3), 3), ls.EXIT_ALERT)

    def test_single_critical_alerts(self):
        self.assertEqual(ls.evaluate(self.run_scan("CRIT\n"), 3), ls.EXIT_ALERT)

    def test_no_readable_file_is_unknown(self):
        result = ls.scan([str(self.tmp / "missing.log")], self.rules, {})
        self.assertEqual(ls.evaluate(result, 3), ls.EXIT_UNKNOWN)
        self.assertEqual(len(result.errors), 1)


class StateTests(TempDirTestCase):
    def setUp(self):
        super().setUp()
        self.rules = [ls.Rule("crit", ls.re.compile("CRIT"), "CRITICAL")]
        self.log = self.write("app.log", "CRIT one\n")

    def test_second_run_only_sees_new_lines(self):
        state = {}
        self.assertEqual(len(ls.scan([str(self.log)], self.rules, state).matches), 1)
        self.assertEqual(len(ls.scan([str(self.log)], self.rules, state).matches), 0)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("CRIT two\n")
        result = ls.scan([str(self.log)], self.rules, state)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].line_no, 2)  # line numbers continue

    def test_rotation_is_detected(self):
        state = {}
        ls.scan([str(self.log)], self.rules, state)
        self.log.rename(self.tmp / "app.log.1")    # logrotate: rename + create new file
        self.write("app.log", "CRIT after rotate\n")
        result = ls.scan([str(self.log)], self.rules, state)
        self.assertEqual(len(result.matches), 1)
        self.assertEqual(result.matches[0].line_no, 1)

    def test_replaced_file_with_reused_inode_is_rescanned(self):
        state = {}
        ls.scan([str(self.log)], self.rules, state)
        self.log.unlink()                          # inode may be reused immediately
        self.write("app.log", "CRIT replaced content\n")
        self.assertEqual(len(ls.scan([str(self.log)], self.rules, state).matches), 1)

    def test_truncated_file_is_rescanned(self):
        state = {}
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write("filler line\n" * 5)
        ls.scan([str(self.log)], self.rules, state)
        self.log.write_text("CRIT new\n", encoding="utf-8")  # same inode, smaller
        self.assertEqual(len(ls.scan([str(self.log)], self.rules, state).matches), 1)

    def test_incomplete_last_line_waits_for_newline(self):
        state = {}
        self.log.write_text("CRIT partial", encoding="utf-8")  # writer not finished yet
        self.assertEqual(len(ls.scan([str(self.log)], self.rules, state).matches), 0)
        with open(self.log, "a", encoding="utf-8") as fh:
            fh.write(" line\n")
        result = ls.scan([str(self.log)], self.rules, state)
        self.assertEqual(result.matches[0].text, "CRIT partial line")

    def test_state_file_roundtrip(self):
        state_file = self.tmp / "state" / "scanner.json"
        state = {}
        ls.scan([str(self.log)], self.rules, state)
        ls.save_state(state_file, state)
        self.assertEqual(ls.load_state(state_file), state)

    def test_corrupt_state_file_is_ignored(self):
        bad = self.write("state.json", "{not json")
        self.assertEqual(ls.load_state(bad), {})


class CliTests(TempDirTestCase):
    def run_main(self, *args):
        out = io.StringIO()
        with redirect_stdout(out):
            code = ls.main(list(args))
        return code, out.getvalue()

    def test_alert_exit_code_and_email_dry_run(self):
        code, out = self.run_main(str(SAMPLES / "deploy.log"), "--email-dry-run")
        self.assertEqual(code, ls.EXIT_ALERT)
        self.assertIn("ALERT", out)
        self.assertIn("e-mail (dry run, not sent)", out)
        self.assertIn("Subject: [ALERT] log-scanner", out)

    def test_quiet_mode_prints_nothing_when_ok(self):
        clean = self.write("clean.log", "nothing to see\n")
        code, out = self.run_main(str(clean), "--quiet")
        self.assertEqual(code, ls.EXIT_OK)
        self.assertEqual(out, "")

    def test_glob_expansion(self):
        code, out = self.run_main(str(SAMPLES / "*.log"))
        self.assertEqual(code, ls.EXIT_ALERT)
        self.assertIn("files scanned: 3", out)

    def test_bad_config_returns_unknown(self):
        bad = self.write("bad.toml", "not = [valid")
        code, _ = self.run_main(str(SAMPLES / "deploy.log"), "-c", str(bad))
        self.assertEqual(code, ls.EXIT_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
