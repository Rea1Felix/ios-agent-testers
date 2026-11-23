"""Unit tests for GitHub issue parsing and bug report fetching/formatting."""

from __future__ import annotations

import json
import subprocess
import unittest
from unittest import mock

from ios_agent_testers.core.bug_report import (
    extract_bug_report_title,
    fetch_or_format_bug_report,
    parse_github_issue_ref,
)


class TestBugReport(unittest.TestCase):
    def test_parse_github_url_and_shorthand(self) -> None:
        self.assertEqual(
            parse_github_issue_ref("https://github.com/acme/my-app/issues/42"),
            ("acme/my-app", 42),
        )
        self.assertEqual(
            parse_github_issue_ref("https://github.com/acme.inc/my_app.ios/issues/7/"),
            ("acme.inc/my_app.ios", 7),
        )
        self.assertEqual(
            parse_github_issue_ref("  acme/my-app#123  "),
            ("acme/my-app", 123),
        )

    def test_parse_rejects_non_issue_strings(self) -> None:
        for raw in (
            "",
            "   ",
            "https://github.com/acme/my-app/pull/42",
            "https://gitlab.com/acme/my-app/issues/42",
            "http://github.com/acme/my-app/issues/42",
            "acme/my-app#0",
            "acme/my-app#42 extra words",
            "acme/my-app#42\nMore details on line 2",
            "../evil#1",
            "App crashes when tapping Submit on iPhone SE",
        ):
            self.assertIsNone(parse_github_issue_ref(raw), msg=f"Should not match: {raw!r}")

    def test_fetch_github_issue_via_gh_api(self) -> None:
        fake_payload = {
            "title": "App crashes on Arabic settings",
            "html_url": "https://github.com/acme/demo/issues/99",
            "body": "Steps:\n1. Open Settings\n2. Tap Language",
        }
        completed = subprocess.CompletedProcess(
            args=["gh", "api", "repos/acme/demo/issues/99"],
            returncode=0,
            stdout=json.dumps(fake_payload),
            stderr="",
        )
        with mock.patch("shutil.which", return_value="/opt/homebrew/bin/gh"):
            with mock.patch("subprocess.run", return_value=completed) as run_mock:
                md = fetch_or_format_bug_report("acme/demo#99")

        run_mock.assert_called_once()
        cmd = run_mock.call_args[0][0]
        self.assertEqual(cmd, ["/opt/homebrew/bin/gh", "api", "repos/acme/demo/issues/99"])
        self.assertEqual(
            md,
            "# App crashes on Arabic settings\n\n"
            "Reported in https://github.com/acme/demo/issues/99\n\n"
            "Steps:\n1. Open Settings\n2. Tap Language\n",
        )
        self.assertEqual(extract_bug_report_title(md), "App crashes on Arabic settings")

    def test_fetch_github_issue_null_body(self) -> None:
        completed = subprocess.CompletedProcess(
            args=["gh", "api", "repos/acme/demo/issues/1"],
            returncode=0,
            stdout=json.dumps(
                {
                    "title": "Blank body issue",
                    "html_url": "https://github.com/acme/demo/issues/1",
                    "body": None,
                }
            ),
            stderr="",
        )
        with mock.patch("shutil.which", return_value="/usr/local/bin/gh"):
            with mock.patch("subprocess.run", return_value=completed):
                md = fetch_or_format_bug_report(
                    "https://github.com/acme/demo/issues/1/"
                )
        self.assertEqual(
            md,
            "# Blank body issue\n\nReported in https://github.com/acme/demo/issues/1\n\n",
        )

    def test_fetch_github_issue_error_cases(self) -> None:
        with mock.patch("shutil.which", return_value=None):
            with self.assertRaises(RuntimeError):
                fetch_or_format_bug_report("acme/demo#5")

        failed = subprocess.CompletedProcess(
            args=["gh", "api", "repos/acme/demo/issues/5"],
            returncode=1,
            stdout="",
            stderr="HTTP 404: Not Found",
        )
        with mock.patch("shutil.which", return_value="/usr/local/bin/gh"):
            with mock.patch("subprocess.run", return_value=failed):
                with self.assertRaises(RuntimeError):
                    fetch_or_format_bug_report("acme/demo#5")

    def test_plain_text_bug_report(self) -> None:
        with mock.patch("subprocess.run") as run_mock:
            md = fetch_or_format_bug_report("Crash when tapping checkout")
        run_mock.assert_not_called()
        self.assertEqual(md, "Crash when tapping checkout\n")

        with self.assertRaises(ValueError):
            fetch_or_format_bug_report("   ")


if __name__ == "__main__":
    unittest.main()
