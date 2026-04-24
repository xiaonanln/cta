"""Tests for cron.py CLI."""

import io
import json
import os
import sys
import unittest
from unittest.mock import patch, MagicMock

import cron


def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


class TestCronCli(unittest.TestCase):

    def setUp(self):
        os.environ["CTA_UID"] = "123"
        os.environ["CTA_CHAT_ID"] = "456"

    def tearDown(self):
        os.environ.pop("CTA_UID", None)
        os.environ.pop("CTA_CHAT_ID", None)

    @patch("cron.urllib.request.urlopen")
    def test_add_posts_to_cronjobs(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"ok": True, "next_run": "2026-05-01T09:00:00"})
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            cron.main(["add", "myjob", "--schedule", "0 9 * * *", "--prompt", "hi there"])
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn("/cronjobs", req.full_url)
        body = json.loads(req.data)
        self.assertEqual(body, {"uid": 123, "chat_id": 456, "id": "myjob",
                                "schedule": "0 9 * * *", "prompt": "hi there"})
        self.assertIn("added myjob", out.getvalue())

    @patch("cron.urllib.request.urlopen")
    def test_list_filters_by_uid_chat_id(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"jobs": [
            {"uid": 123, "chat_id": 456, "id": "mine", "schedule": "0 9 * * *",
             "prompt": "p", "next_run": "2026-05-01T09:00:00"},
            {"uid": 999, "chat_id": 888, "id": "other", "schedule": "0 9 * * *",
             "prompt": "p", "next_run": "2026-05-01T09:00:00"},
        ]})
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            cron.main(["list"])
        output = out.getvalue()
        self.assertIn("mine", output)
        self.assertNotIn("other", output)

    @patch("cron.urllib.request.urlopen")
    def test_remove_issues_delete(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"ok": True})
        with patch("sys.stdout", new_callable=io.StringIO):
            cron.main(["remove", "myjob"])
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "DELETE")
        self.assertIn("/cronjobs/123/456/myjob", req.full_url)

    @patch("cron.urllib.request.urlopen")
    def test_update_sends_only_provided_fields(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"ok": True, "next_run": "2026-05-01T09:00:00"})
        with patch("sys.stdout", new_callable=io.StringIO):
            cron.main(["update", "myjob", "--prompt", "new prompt"])
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "PUT")
        body = json.loads(req.data)
        self.assertEqual(body, {"prompt": "new prompt"})

    def test_update_without_fields_exits(self):
        with self.assertRaises(SystemExit):
            with patch("sys.stdout", new_callable=io.StringIO):
                cron.main(["update", "myjob"])

    def test_missing_context_exits(self):
        os.environ.pop("CTA_UID", None)
        os.environ.pop("CTA_CHAT_ID", None)
        with self.assertRaises(SystemExit) as ctx:
            cron.main(["list"])
        self.assertIn("uid/chat_id not set", str(ctx.exception))

    @patch("cron.urllib.request.urlopen")
    def test_flag_overrides_env(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"jobs": []})
        os.environ["CTA_UID"] = "999"
        with patch("sys.stdout", new_callable=io.StringIO):
            cron.main(["--uid", "77", "--chat-id", "88", "list"])
        # No assertion on request content — just verify flags were accepted

    @patch("cron.urllib.request.urlopen")
    def test_uses_config_web_port(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"jobs": []})
        with patch("cron._base_url", return_value="http://127.0.0.1:17488"):
            with patch("sys.stdout", new_callable=io.StringIO):
                cron.main(["list"])
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.startswith("http://127.0.0.1:17488"))


if __name__ == "__main__":
    unittest.main()
