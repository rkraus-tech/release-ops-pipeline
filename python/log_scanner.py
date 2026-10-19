#!/usr/bin/env python3
"""
log_scanner.py - scan log files for error patterns and raise alerts.

Standalone tool (Python standard library only, Python 3.11+).

Features
  * rules (regex + severity + optional file filter) in a TOML file
  * scans many files at once, globs allowed ("/var/log/nginx/*.log")
  * optional state file: only NEW lines are scanned on the next run,
    log rotation is detected (inode change or file got smaller)
  * alert policy: any CRITICAL match, or WARNING matches >= threshold
  * alert channels: console (always) and e-mail (SMTP + STARTTLS)
  * Nagios-style exit codes, usable from cron, systemd or Jenkins

Exit codes
  0  OK        no relevant matches
  1  WARNING   warnings found, but below the alert threshold
  2  ALERT     critical match or warning threshold reached
  3  UNKNOWN   tool error (bad config, no readable log file, ...)

E-mail settings come from environment variables (never from the repo):
  SMTP_HOST, SMTP_PORT (default 587), SMTP_USER, SMTP_PASSWORD,
  ALERT_FROM, ALERT_TO (comma separated)
"""

from __future__ import annotations

import argparse
import fnmatch
import glob
import hashlib
import json
import os
import re
import smtplib
import socket
import sys
import tempfile
import tomllib
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime
from email.message import EmailMessage
from pathlib import Path

EXIT_OK, EXIT_WARNING, EXIT_ALERT, EXIT_UNKNOWN = 0, 1, 2, 3
SEVERITIES = ("INFO", "WARNING", "CRITICAL")
DEFAULT_RULES = Path(__file__).with_name("rules.toml")
FINGERPRINT_BYTES = 64


class ConfigError(Exception):
    """Raised for invalid rule files."""


# --------------------------------------------------------------------------- #
# Data model
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class Rule:
    name: str
    regex: re.Pattern
    severity: str
    files: tuple[str, ...] = ()

    def applies_to(self, path: str) -> bool:
        """A rule without file filter applies to every file."""
        if not self.files:
            return True
        name = os.path.basename(path)
        return any(fnmatch.fnmatch(name, pat) for pat in self.files)


@dataclass
class Match:
    rule: str
    severity: str
    path: str
    line_no: int
    text: str


@dataclass
class ScanResult:
    matches: list[Match] = field(default_factory=list)
    scanned_files: list[str] = field(default_factory=list)
    lines_scanned: int = 0
    errors: list[str] = field(default_factory=list)

    def count(self, severity: str) -> int:
        return sum(1 for m in self.matches if m.severity == severity)

    def by_rule(self) -> Counter:
        return Counter((m.severity, m.rule) for m in self.matches)


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
def load_config(path: Path) -> tuple[list[Rule], int]:
    """Load and validate the rule file. Returns (rules, warning_threshold)."""
    try:
        with open(path, "rb") as fh:
            data = tomllib.load(fh)
    except FileNotFoundError:
        raise ConfigError(f"rule file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"invalid TOML in {path}: {exc}") from None

    threshold = data.get("alerting", {}).get("warning_threshold", 3)
    if not isinstance(threshold, int) or threshold < 1:
        raise ConfigError("alerting.warning_threshold must be an integer >= 1")

    rules: list[Rule] = []
    seen: set[str] = set()
    for i, raw in enumerate(data.get("rules", []), start=1):
        name = raw.get("name")
        pattern = raw.get("pattern")
        severity = str(raw.get("severity", "")).upper()
        if not name or not pattern:
            raise ConfigError(f"rule #{i}: 'name' and 'pattern' are required")
        if name in seen:
            raise ConfigError(f"rule '{name}' is defined twice")
        if severity not in SEVERITIES:
            raise ConfigError(f"rule '{name}': severity must be one of {SEVERITIES}")
        try:
            regex = re.compile(pattern)
        except re.error as exc:
            raise ConfigError(f"rule '{name}': invalid regex: {exc}") from None
        seen.add(name)
        rules.append(Rule(name, regex, severity, tuple(raw.get("files", []))))

    if not rules:
        raise ConfigError(f"no rules defined in {path}")
    return rules, threshold


# --------------------------------------------------------------------------- #
# State handling (scan only new lines)
# --------------------------------------------------------------------------- #
def load_state(path: Path | None) -> dict:
    if path is None or not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}  # corrupt state -> start from scratch rather than crash


def save_state(path: Path | None, state: dict) -> None:
    """Write atomically: temp file + rename, so a crash never leaves half a file."""
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".state-")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, sort_keys=True)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- #
# Scanning
# --------------------------------------------------------------------------- #
def expand_paths(patterns: list[str]) -> list[str]:
    """Expand globs; keep non-matching literal paths so missing files are reported."""
    paths: list[str] = []
    for pat in patterns:
        hits = sorted(glob.glob(pat))
        paths.extend(hits if hits else [pat])
    return list(dict.fromkeys(paths))  # de-duplicate, keep order


def file_fingerprint(path: str, size: int) -> str:
    """Hash of the first already-scanned bytes - detects a replaced file even if
    the file system reuses the inode number."""
    try:
        with open(path, "rb") as fh:
            return hashlib.sha256(fh.read(size)).hexdigest()[:16]
    except OSError:
        return ""


def scan_file(path: str, rules: list[Rule], state: dict, result: ScanResult) -> None:
    applicable = [r for r in rules if r.applies_to(path)]
    try:
        st = os.stat(path)
    except OSError as exc:
        result.errors.append(f"{path}: {exc.strerror}")
        return

    prev = state.get(path, {})
    offset, line_no = prev.get("offset", 0), prev.get("line", 0)
    fp_len = prev.get("fp_len", 0)
    if (prev.get("inode") != st.st_ino                                  # file replaced (logrotate)
            or st.st_size < offset                                      # file truncated
            or prev.get("fingerprint") != file_fingerprint(path, fp_len)):  # inode reused
        offset, line_no = 0, 0                                          # -> start from the top

    try:
        with open(path, "rb") as fh:
            fh.seek(offset)
            for raw in fh:
                if not raw.endswith(b"\n"):
                    break  # incomplete last line: read it next time when complete
                offset += len(raw)
                line_no += 1
                result.lines_scanned += 1
                line = raw.decode("utf-8", errors="replace").rstrip("\r\n")
                for rule in applicable:
                    if rule.regex.search(line):
                        result.matches.append(
                            Match(rule.name, rule.severity, path, line_no, line))
    except OSError as exc:
        result.errors.append(f"{path}: {exc.strerror}")
        return

    fp_len = min(FINGERPRINT_BYTES, offset)
    state[path] = {"inode": st.st_ino, "offset": offset, "line": line_no,
                   "fp_len": fp_len, "fingerprint": file_fingerprint(path, fp_len)}
    result.scanned_files.append(path)


def scan(paths: list[str], rules: list[Rule], state: dict) -> ScanResult:
    result = ScanResult()
    for path in paths:
        scan_file(path, rules, state, result)
    return result


# --------------------------------------------------------------------------- #
# Evaluation and reporting
# --------------------------------------------------------------------------- #
def evaluate(result: ScanResult, threshold: int) -> int:
    if not result.scanned_files:
        return EXIT_UNKNOWN
    if result.count("CRITICAL") > 0 or result.count("WARNING") >= threshold:
        return EXIT_ALERT
    if result.count("WARNING") > 0:
        return EXIT_WARNING
    return EXIT_OK


STATUS_TEXT = {EXIT_OK: "OK", EXIT_WARNING: "WARNING",
               EXIT_ALERT: "ALERT", EXIT_UNKNOWN: "UNKNOWN"}


def build_report(result: ScanResult, status: int, threshold: int, max_examples: int) -> str:
    host = socket.gethostname()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    lines = [
        f"[log-scanner] {STATUS_TEXT[status]} on {host} at {now}",
        f"files scanned: {len(result.scanned_files)}, new lines: {result.lines_scanned}, "
        f"CRITICAL: {result.count('CRITICAL')}, WARNING: {result.count('WARNING')} "
        f"(threshold {threshold}), INFO: {result.count('INFO')}",
    ]
    for err in result.errors:
        lines.append(f"  ! cannot read {err}")

    if result.matches:
        lines.append("")
        lines.append("matches per rule:")
        order = {s: i for i, s in enumerate(reversed(SEVERITIES))}
        for (sev, rule), n in sorted(result.by_rule().items(), key=lambda kv: (order[kv[0][0]], kv[0][1])):
            lines.append(f"  {sev:<8} {rule:<26} {n:>4}x")

        lines.append("")
        lines.append(f"examples (max {max_examples}, most severe first):")
        ranked = sorted(result.matches, key=lambda m: order[m.severity])
        seen, examples = set(), []
        for m in ranked:  # one example per log line, even if several rules matched
            if (m.path, m.line_no) not in seen:
                seen.add((m.path, m.line_no))
                examples.append(m)
        for m in examples[:max_examples]:
            text = m.text if len(m.text) <= 160 else m.text[:157] + "..."
            lines.append(f"  [{m.severity}] {m.path}:{m.line_no}  {text}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# Alert channels
# --------------------------------------------------------------------------- #
def build_email(report: str, status: int) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = f"[{STATUS_TEXT[status]}] log-scanner on {socket.gethostname()}"
    msg["From"] = os.environ.get("ALERT_FROM", f"log-scanner@{socket.gethostname()}")
    msg["To"] = os.environ.get("ALERT_TO", "")
    msg.set_content(report + "\n")
    return msg


def send_email(msg: EmailMessage) -> None:
    host = os.environ.get("SMTP_HOST")
    if not host or not msg["To"]:
        raise RuntimeError("SMTP_HOST and ALERT_TO must be set for e-mail alerts")
    port = int(os.environ.get("SMTP_PORT", "587"))
    with smtplib.SMTP(host, port, timeout=15) as smtp:
        smtp.starttls()
        user, password = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD")
        if user and password:
            smtp.login(user, password)
        smtp.send_message(msg)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Scan log files for error patterns and raise alerts.")
    p.add_argument("files", nargs="+", help="log files or glob patterns (quote globs)")
    p.add_argument("-c", "--config", type=Path, default=DEFAULT_RULES, help="rule file (TOML)")
    p.add_argument("-s", "--state", type=Path, help="state file: only scan lines added since the last run")
    p.add_argument("-n", "--max-examples", type=int, default=10, help="example lines in the report")
    p.add_argument("--email", action="store_true", help="send an e-mail when an ALERT is raised")
    p.add_argument("--email-dry-run", action="store_true", help="print the alert e-mail instead of sending it")
    p.add_argument("-q", "--quiet", action="store_true", help="print the report only for WARNING/ALERT")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        rules, threshold = load_config(args.config)
    except ConfigError as exc:
        print(f"[log-scanner] config error: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN

    state = load_state(args.state)
    result = scan(expand_paths(args.files), rules, state)
    status = evaluate(result, threshold)
    report = build_report(result, status, threshold, args.max_examples)

    if not (args.quiet and status == EXIT_OK):
        print(report)

    if status == EXIT_ALERT and (args.email or args.email_dry_run):
        msg = build_email(report, status)
        if args.email_dry_run:
            print("\n----- e-mail (dry run, not sent) -----")
            print(msg.as_string())
        else:
            try:
                send_email(msg)
                print(f"[log-scanner] alert e-mail sent to {msg['To']}")
            except (OSError, smtplib.SMTPException, RuntimeError) as exc:
                print(f"[log-scanner] e-mail could NOT be sent: {exc}", file=sys.stderr)

    # Save state only after a successful run, so no lines are lost on errors
    if status != EXIT_UNKNOWN:
        save_state(args.state, state)
    return status


if __name__ == "__main__":
    sys.exit(main())
