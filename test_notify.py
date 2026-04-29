"""Tests for notify.py CLI."""

import io
import json
import unittest
from unittest.mock import patch, MagicMock

import notify


def _fake_response(payload):
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.__enter__ = lambda s: s
    resp.__exit__ = lambda *a: None
    return resp


class TestNotifyCli(unittest.TestCase):

    @patch("notify.urllib.request.urlopen")
    def test_send_with_explicit_uid_and_chat_id(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"ok": True})
        with patch("sys.stdout", new_callable=io.StringIO):
            notify.main(["send", "hello there", "--uid", "123", "--chat-id", "456"])
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(req.method, "POST")
        self.assertIn("/chat/123/456/send", req.full_url)
        body = json.loads(req.data)
        self.assertEqual(body, {"text": "hello there"})

    @patch("notify.urllib.request.urlopen")
    def test_send_with_to_label_resolves_via_chats(self, mock_urlopen):
        # First call: GET /chats returns chats; second call: POST send
        mock_urlopen.side_effect = [
            _fake_response({"chats": [
                {"label": "CTA", "uid": 100, "chat_id": -200},
                {"label": "OtherChat", "uid": 100, "chat_id": -300},
            ]}),
            _fake_response({"ok": True}),
        ]
        with patch("sys.stdout", new_callable=io.StringIO):
            notify.main(["send", "ping", "--to", "CTA"])
        # Second call should have hit the right chat
        post_req = mock_urlopen.call_args_list[1][0][0]
        self.assertIn("/chat/100/-200/send", post_req.full_url)
        self.assertEqual(json.loads(post_req.data), {"text": "ping"})

    @patch("notify.urllib.request.urlopen")
    def test_send_to_unknown_label_exits(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"chats": [
            {"label": "OtherChat", "uid": 100, "chat_id": -300},
        ]})
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new_callable=io.StringIO):
                notify.main(["send", "x", "--to", "Nonexistent"])
        self.assertIn("no chat with label", str(ctx.exception))

    @patch("notify.urllib.request.urlopen")
    def test_send_to_ambiguous_label_exits(self, mock_urlopen):
        """Two chats with the same label is ambiguous — must error rather than guess."""
        mock_urlopen.return_value = _fake_response({"chats": [
            {"label": "Bot", "uid": 100, "chat_id": -1},
            {"label": "Bot", "uid": 100, "chat_id": -2},
        ]})
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new_callable=io.StringIO):
                notify.main(["send", "x", "--to", "Bot"])
        self.assertIn("multiple chats", str(ctx.exception))

    def test_send_without_recipient_exits(self):
        with self.assertRaises(SystemExit) as ctx:
            with patch("sys.stdout", new_callable=io.StringIO):
                notify.main(["send", "x"])
        self.assertIn("--to", str(ctx.exception))

    @patch("notify.urllib.request.urlopen")
    def test_list_prints_all_chats(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"chats": [
            {"label": "CTA", "uid": 100, "chat_id": -200},
            {"label": "GoVerse", "uid": 100, "chat_id": -300},
        ]})
        with patch("sys.stdout", new_callable=io.StringIO) as out:
            notify.main(["list"])
        output = out.getvalue()
        self.assertIn("CTA", output)
        self.assertIn("GoVerse", output)
        self.assertIn("100", output)

    @patch("notify.urllib.request.urlopen")
    def test_uses_config_web_port(self, mock_urlopen):
        mock_urlopen.return_value = _fake_response({"chats": []})
        with patch("notify._base_url", return_value="http://127.0.0.1:17488"):
            with patch("sys.stdout", new_callable=io.StringIO):
                notify.main(["list"])
        req = mock_urlopen.call_args[0][0]
        self.assertTrue(req.full_url.startswith("http://127.0.0.1:17488"))


if __name__ == "__main__":
    unittest.main()
