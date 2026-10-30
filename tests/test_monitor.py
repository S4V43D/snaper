"""Offline tests: never fetch a real page or send mail."""

import copy
import json
import os
from pathlib import Path
import smtplib
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests
import monitor


CONFIG = {"url": "https://example.com/", "selector": "#announcements",
          "ignore_selectors": [".timestamp"], "access_permission_confirmed": True}
MAIL = {"GMAIL_USERNAME": "sender@example.com", "GMAIL_APP_PASSWORD": "dummy-password",
        "ALERT_TO": "recipient@example.com"}


class StateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / "nested" / "state.json"
        self.config = copy.deepcopy(CONFIG)
        self.env = patch.dict(os.environ, MAIL)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.fetch = patch("monitor.fetch_text", return_value="Office opens Monday").start()
        self.mail = patch("monitor.send_email").start()
        self.addCleanup(patch.stopall)

    def baseline(self):
        return monitor.run(self.config, self.path)

    def test_first_run_saves_without_email(self):
        self.assertIn("Baseline saved", self.baseline())
        self.assertEqual(monitor.load_state(self.path)["text"], "Office opens Monday")
        self.mail.assert_not_called()

    def test_unchanged_preserves_exact_state(self):
        self.baseline()
        before = self.path.read_bytes()
        self.assertIn("Unchanged", self.baseline())
        self.assertEqual(before, self.path.read_bytes())
        self.mail.assert_not_called()

    def test_change_emails_once_and_updates_state(self):
        self.baseline()
        self.fetch.return_value = "Office opens Tuesday"
        self.assertIn("Change email accepted", self.baseline())
        self.assertEqual(monitor.load_state(self.path)["text"], "Office opens Tuesday")
        self.assertIn("Office opens Monday", self.mail.call_args.args[1])
        self.assertIn("Office opens Tuesday", self.mail.call_args.args[1])
        self.baseline()
        self.mail.assert_called_once()

    def test_email_failure_keeps_previous_state_for_retry(self):
        self.baseline()
        before = self.path.read_bytes()
        self.fetch.return_value = "Updated opening hours"
        self.mail.side_effect = smtplib.SMTPException("offline")
        with self.assertRaises(smtplib.SMTPException):
            self.baseline()
        self.assertEqual(before, self.path.read_bytes())
        self.mail.side_effect = None
        self.assertIn("Change email accepted", self.baseline())

    def test_fetch_failure_keeps_state(self):
        self.baseline()
        before = self.path.read_bytes()
        self.fetch.side_effect = requests.Timeout()
        with self.assertRaises(requests.Timeout):
            self.baseline()
        self.assertEqual(before, self.path.read_bytes())

    def test_dry_run_has_no_side_effects(self):
        with patch.dict(os.environ, {"GMAIL_USERNAME": "", "GMAIL_APP_PASSWORD": "", "ALERT_TO": ""}):
            self.assertIn("DRY RUN: baseline", monitor.run(self.config, self.path, dry_run=True))
        self.assertFalse(self.path.exists())
        self.mail.assert_not_called()

    def test_dry_run_change_does_not_advance_state(self):
        self.baseline()
        before = self.path.read_bytes()
        self.fetch.return_value = "Different text"
        self.assertIn("DRY RUN: changed", monitor.run(self.config, self.path, dry_run=True))
        self.assertEqual(before, self.path.read_bytes())
        self.mail.assert_not_called()

    def test_changed_target_requires_explicit_reset(self):
        self.baseline()
        self.config["selector"] = "main"
        with self.assertRaisesRegex(monitor.MonitorError, "reset"):
            self.baseline()
        self.assertIn("Baseline saved", monitor.run(self.config, self.path, reset=True))
        self.mail.assert_not_called()

    def test_corrupt_state_is_not_silently_reset(self):
        self.baseline()
        self.path.write_text("{bad json", encoding="utf-8")
        with self.assertRaisesRegex(monitor.MonitorError, "invalid"):
            self.baseline()
        self.assertEqual(self.path.read_text(), "{bad json")

    def test_state_checksum_mismatch_is_rejected(self):
        self.baseline()
        state = json.loads(self.path.read_text())
        state["text"] = "tampered"
        self.path.write_text(json.dumps(state))
        with self.assertRaises(monitor.MonitorError):
            self.baseline()

    def test_permission_required_before_fetch(self):
        self.config["access_permission_confirmed"] = False
        with self.assertRaisesRegex(monitor.MonitorError, "access rules"):
            self.baseline()
        self.fetch.assert_not_called()

    def test_missing_mail_settings_fail_before_fetch(self):
        with patch.dict(os.environ, {"GMAIL_APP_PASSWORD": ""}):
            with self.assertRaises(monitor.MonitorError):
                self.baseline()
        self.fetch.assert_not_called()

    def test_atomic_write_failure_keeps_original(self):
        self.baseline()
        before = self.path.read_bytes()
        with patch("monitor.os.replace", side_effect=OSError("disk unavailable")):
            with self.assertRaises(OSError):
                monitor.save_state(self.path, self.config, "Changed")
        self.assertEqual(before, self.path.read_bytes())
        self.assertEqual(list(self.path.parent.glob("*.tmp")), [])


class ParsingTests(unittest.TestCase):
    def test_normalizes_text_and_excludes_noise(self):
        html = '<main id="announcements"> Office\n opens <b>Monday</b><i class="timestamp">12:03</i><script>code</script><!-- comment --></main>'
        self.assertEqual(monitor.extract_text(html, CONFIG), "Office opens Monday")

    def test_absent_multiple_and_empty_sections_fail(self):
        for html in ("<p>Wrong page</p>", '<i id="announcements">A</i><i id="announcements">B</i>',
                     '<main id="announcements"><script>nothing</script></main>'):
            with self.subTest(html=html), self.assertRaises(monitor.MonitorError):
                monitor.extract_text(html, CONFIG)

    def test_environment_overrides(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            path.write_text(json.dumps(CONFIG))
            with patch.dict(os.environ, {"MONITOR_URL": "https://example.org/", "MONITOR_SELECTOR": "h1"}):
                config = monitor.load_config(path)
            self.assertEqual(config["url"], "https://example.org/")
            self.assertEqual(config["selector"], "h1")


class AccessTests(unittest.TestCase):
    @patch("monitor.socket.getaddrinfo", return_value=[(2, 1, 6, "", ("127.0.0.1", 443))])
    def test_private_address_rejected(self, _):
        with self.assertRaises(monitor.MonitorError):
            monitor.validate_url("https://example.com/")

    def test_unsafe_urls_rejected(self):
        for url in ("http://example.com/", "https://name:pass@example.com/", "file:///secret",
                    "https://example.com:8443/", "https://example.com/#section"):
            with self.subTest(url=url), self.assertRaises(monitor.MonitorError):
                monitor.validate_url(url)

    @patch("monitor.get_resource", return_value="User-agent: *\nDisallow: /\n")
    def test_robots_denial(self, _):
        with self.assertRaisesRegex(monitor.MonitorError, "disallows"):
            monitor.check_robots(MagicMock(), CONFIG["url"])

    @patch("monitor.get_resource", return_value=None)
    def test_missing_robots_is_handled(self, _):
        monitor.check_robots(MagicMock(), CONFIG["url"])

    @patch("monitor.get_resource", return_value="User-agent: *\nCrawl-delay: 901\n")
    def test_overlong_crawl_delay_blocks_fetch(self, _):
        with self.assertRaisesRegex(monitor.MonitorError, "crawl delay"):
            monitor.check_robots(MagicMock(), CONFIG["url"])

    @patch("monitor.validate_url")
    def test_http_failures_and_redirects_are_not_consumed(self, _):
        for status in (301, 401, 403, 429, 500):
            session = MagicMock()
            response = session.get.return_value.__enter__.return_value
            response.status_code = status
            with self.subTest(status=status), self.assertRaises(monitor.MonitorError):
                monitor.get_resource(session, CONFIG["url"])
            self.assertFalse(session.get.call_args.kwargs["allow_redirects"])
            response.iter_content.assert_not_called()

    @patch("monitor.validate_url")
    def test_non_html_and_oversized_responses_rejected(self, _):
        session = MagicMock()
        response = session.get.return_value.__enter__.return_value
        response.status_code = 200
        response.headers = {"Content-Type": "application/json"}
        with self.assertRaisesRegex(monitor.MonitorError, "content type"):
            monitor.get_resource(session, CONFIG["url"])
        response.headers = {"Content-Type": "text/html"}
        response.iter_content.return_value = [b"x" * (monitor.MAX_BYTES + 1)]
        with self.assertRaisesRegex(monitor.MonitorError, "size or time"):
            monitor.get_resource(session, CONFIG["url"])


class EmailTests(unittest.TestCase):
    @patch.dict(os.environ, MAIL)
    @patch("monitor.smtplib.SMTP_SSL")
    def test_tls_login_and_plain_text_message(self, smtp_class):
        smtp = smtp_class.return_value.__enter__.return_value
        smtp.send_message.return_value = {}
        monitor.send_email("Setup test", "Body")
        self.assertEqual(smtp_class.call_args.args[:2], ("smtp.gmail.com", 465))
        smtp.login.assert_called_once_with(MAIL["GMAIL_USERNAME"], MAIL["GMAIL_APP_PASSWORD"])
        message = smtp.send_message.call_args.args[0]
        self.assertEqual(message.get_content_type(), "text/plain")
        self.assertEqual(message["To"], MAIL["ALERT_TO"])


if __name__ == "__main__":
    unittest.main()
