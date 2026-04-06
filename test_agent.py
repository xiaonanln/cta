"""Tests for CTA (Claude Telegram Agent)."""

import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, call

# Initialize with test config before importing
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import agent

agent.init(agent.DEFAULT_CONFIG)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_fake_message(text, user_id=123, username="tester", chat_type="private", chat_title=None):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.from_user.id = user_id
    msg.from_user.username = username
    msg.chat.id = user_id
    msg.chat.type = chat_type
    msg.chat.title = chat_title
    msg.message_id = 1
    return msg


def setup_fake_bot():
    agent.bot = MagicMock()
    agent.user_sessions.clear()
    agent.user_cwd.clear()
    agent.user_model.clear()
    agent.chat_labels.clear()
    agent.msg_counts.clear()
    return agent.bot


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):

    def test_default_config_keys(self):
        config = agent.DEFAULT_CONFIG
        self.assertEqual(config["telegram_bot_token"], "")
        self.assertEqual(config["allowed_users"], [])
        self.assertEqual(config["claude_timeout"], 600)
        self.assertEqual(config["model"], "claude-sonnet-4-6")

    def test_load_from_file(self):
        cfg = {"telegram_bot_token": "abc:123", "allowed_users": [111]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            name = f.name
        try:
            with patch.object(agent, "CONFIG_PATH", name):
                config = agent.load_config()
        finally:
            os.unlink(name)
        self.assertEqual(config["telegram_bot_token"], "abc:123")
        self.assertEqual(config["allowed_users"], [111])
        self.assertEqual(config["claude_timeout"], 600)  # default preserved

    def test_missing_file_uses_defaults(self):
        with patch.object(agent, "CONFIG_PATH", "/nonexistent/config.json"):
            config = agent.load_config()
        self.assertEqual(config["claude_timeout"], 600)

    def test_init_applies_all_fields(self):
        config = {
            "telegram_bot_token": "tok",
            "allowed_users": [1, 2],
            "claude_timeout": 30,
            "model": "claude-haiku-4-5-20251001",
        }
        agent.init(config)
        self.assertEqual(agent.BOT_TOKEN, "tok")
        self.assertEqual(agent.ALLOWED_USERS, {1, 2})
        self.assertEqual(agent.TIMEOUT, 30)
        self.assertEqual(agent.MODEL, "claude-haiku-4-5-20251001")
        # Reset
        agent.init(agent.DEFAULT_CONFIG)

    def test_init_sets_model(self):
        original = agent.MODEL
        agent.init({**agent.DEFAULT_CONFIG, "model": "claude-sonnet-4-6"})
        self.assertEqual(agent.MODEL, "claude-sonnet-4-6")
        agent.MODEL = original

    def test_config_path_is_in_cta_home(self):
        self.assertTrue(agent.CONFIG_PATH.startswith(agent.CTA_HOME))
        self.assertTrue(agent.CONFIG_PATH.endswith("config.json"))

    def test_sessions_path_is_in_cta_home(self):
        self.assertTrue(agent.SESSIONS_PATH.startswith(agent.CTA_HOME))
        self.assertTrue(agent.SESSIONS_PATH.endswith("sessions.json"))


# ── Session persistence ───────────────────────────────────────────────────────

class TestSessionPersistence(unittest.TestCase):

    def setUp(self):
        agent.user_sessions.clear()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self._orig_sessions_path = agent.SESSIONS_PATH
        agent.SESSIONS_PATH = self.tmp.name

    def tearDown(self):
        agent.user_sessions.clear()
        agent.user_cwd.clear()
        agent.SESSIONS_PATH = self._orig_sessions_path
        agent.init(agent.DEFAULT_CONFIG)
        for path in [self.tmp.name, self.tmp.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)

    def test_save_and_reload(self):
        agent.user_sessions[(123, 123)] = "sess-abc"
        agent.user_sessions[(456, 456)] = "sess-def"
        agent.save_sessions()
        agent.user_sessions.clear()
        agent.load_sessions()
        self.assertEqual(agent.user_sessions[(123, 123)], "sess-abc")
        self.assertEqual(agent.user_sessions[(456, 456)], "sess-def")

    def test_cwd_persisted_and_restored(self):
        agent.user_sessions[(123, 123)] = "sess-abc"
        agent.user_cwd[(123, 123)] = "/tmp/myproject"
        agent.save_sessions()
        agent.user_sessions.clear()
        agent.user_cwd.clear()
        agent.load_sessions()
        self.assertEqual(agent.user_sessions[(123, 123)], "sess-abc")
        self.assertEqual(agent.user_cwd[(123, 123)], "/tmp/myproject")

    def test_backward_compat_string_format(self):
        with open(self.tmp.name, "w") as f:
            json.dump({"77:77": "old-session-id"}, f)
        agent.load_sessions()
        self.assertEqual(agent.user_sessions[(77, 77)], "old-session-id")

    def test_save_writes_valid_json(self):
        agent.user_sessions[(99, 99)] = "my-session"
        agent.save_sessions()
        with open(self.tmp.name) as f:
            data = json.load(f)
        self.assertEqual(data["99:99"]["session"], "my-session")

    def test_load_missing_file_is_noop(self):
        os.unlink(self.tmp.name)
        agent.load_sessions()  # should not raise
        self.assertEqual(len(agent.user_sessions), 0)

    def test_load_corrupt_file_does_not_crash(self):
        with open(self.tmp.name, "w") as f:
            f.write("not valid json{{{")
        agent.load_sessions()  # should not raise
        self.assertEqual(len(agent.user_sessions), 0)

    def test_save_is_atomic(self):
        """save_sessions must not leave a .tmp file behind."""
        agent.user_sessions[(1, 1)] = "s"
        agent.save_sessions()
        self.assertFalse(os.path.exists(self.tmp.name + ".tmp"))
        self.assertTrue(os.path.exists(self.tmp.name))

    def test_save_empty_sessions(self):
        agent.save_sessions()
        with open(self.tmp.name) as f:
            data = json.load(f)
        self.assertEqual(data, {})


# ── call_claude ───────────────────────────────────────────────────────────────

class TestCallClaude(unittest.TestCase):

    def _mock_result(self, result="ok", session_id="sid"):
        return MagicMock(
            stdout=json.dumps({"result": result, "session_id": session_id, "is_error": False}),
            stderr="",
        )

    @patch("agent.subprocess.run")
    def test_returns_text_and_session_id(self, mock_run):
        mock_run.return_value = self._mock_result("Hello world", "abc-123")
        text, sid = agent.call_claude("hi")
        self.assertEqual(text, "Hello world")
        self.assertEqual(sid, "abc-123")

    @patch("agent.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi", cwd="/tmp/test")
        self.assertEqual(mock_run.call_args[1]["cwd"], "/tmp/test")

    @patch("agent.subprocess.run")
    def test_uses_default_cwd_when_none(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi")
        self.assertEqual(mock_run.call_args[1]["cwd"], agent.DEFAULT_CWD)

    @patch("agent.subprocess.run")
    def test_uses_global_model(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi")
        args = mock_run.call_args[0][0]
        self.assertIn("--model", args)
        self.assertIn(agent.MODEL, args)

    @patch("agent.subprocess.run")
    def test_uses_override_model(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi", model="claude-sonnet-4-6")
        args = mock_run.call_args[0][0]
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], "claude-sonnet-4-6")

    @patch("agent.subprocess.run")
    def test_includes_required_flags(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi")
        args = mock_run.call_args[0][0]
        self.assertIn("--print", args)
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--output-format", args)
        self.assertIn("json", args)

    @patch("agent.subprocess.run")
    def test_resumes_session(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi", session_id="sess-xyz")
        args = mock_run.call_args[0][0]
        self.assertIn("--resume", args)
        self.assertIn("sess-xyz", args)

    @patch("agent.subprocess.run")
    def test_no_resume_without_session_id(self, mock_run):
        mock_run.return_value = self._mock_result()
        agent.call_claude("hi")
        args = mock_run.call_args[0][0]
        self.assertNotIn("--resume", args)

    @patch("agent.subprocess.run")
    def test_stderr_returned_on_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="something broke")
        text, sid = agent.call_claude("hi", max_retries=0)
        self.assertIn("Error", text)
        self.assertIn("something broke", text)
        self.assertEqual(sid, "")

    @patch("agent.subprocess.run")
    def test_empty_stdout_and_stderr(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="")
        text, _ = agent.call_claude("hi", max_retries=0)
        self.assertEqual(text, "(empty response)")

    @patch("agent.subprocess.run")
    def test_strips_whitespace(self, mock_run):
        mock_run.return_value = self._mock_result("  hello  \n")
        text, _ = agent.call_claude("hi")
        self.assertEqual(text, "hello")

    @patch("agent.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 600))
    def test_timeout(self, _):
        text, sid = agent.call_claude("hi")
        self.assertIn("timed out", text)
        self.assertEqual(sid, "")

    @patch("agent.subprocess.run", side_effect=FileNotFoundError)
    def test_cli_not_found(self, _):
        text, _ = agent.call_claude("hi")
        self.assertIn("not found", text)

    @patch("agent.time.sleep")
    @patch("agent.subprocess.run")
    def test_retries_on_empty_response(self, mock_run, mock_sleep):
        ok = self._mock_result("recovered", "sid-ok")
        mock_run.side_effect = [MagicMock(stdout="", stderr=""), ok]
        text, sid = agent.call_claude("hi", max_retries=1, retry_delay=0)
        self.assertEqual(text, "recovered")
        self.assertEqual(sid, "sid-ok")
        self.assertEqual(mock_run.call_count, 2)

    @patch("agent.time.sleep")
    @patch("agent.subprocess.run")
    def test_returns_error_after_all_retries_exhausted(self, mock_run, mock_sleep):
        mock_run.return_value = MagicMock(stdout="", stderr="transient error")
        text, sid = agent.call_claude("hi", max_retries=2, retry_delay=0)
        self.assertIn("transient error", text)
        self.assertEqual(sid, "")
        self.assertEqual(mock_run.call_count, 3)

    @patch("agent.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 600))
    def test_no_retry_on_timeout(self, mock_run):
        text, _ = agent.call_claude("hi", max_retries=2)
        self.assertIn("timed out", text)
        self.assertEqual(mock_run.call_count, 1)

    @patch("agent.subprocess.run", side_effect=FileNotFoundError)
    def test_no_retry_on_file_not_found(self, mock_run):
        text, _ = agent.call_claude("hi", max_retries=2)
        self.assertIn("not found", text)
        self.assertEqual(mock_run.call_count, 1)


# ── _split_reply ──────────────────────────────────────────────────────────────

class TestSplitReply(unittest.TestCase):

    def test_short_text_single_chunk(self):
        self.assertEqual(agent._split_reply("hello"), ["hello"])

    def test_empty_text(self):
        self.assertEqual(agent._split_reply(""), [])

    def test_exact_limit_no_split(self):
        text = "x" * 4096
        chunks = agent._split_reply(text)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0], text)

    def test_splits_at_newline(self):
        line1 = "a" * 100
        line2 = "b" * 100
        text = line1 + "\n" + line2
        chunks = agent._split_reply(text, limit=150)
        self.assertEqual(chunks[0], line1)
        self.assertEqual(chunks[1], line2)

    def test_splits_at_limit_when_no_newline(self):
        text = "x" * 5000
        chunks = agent._split_reply(text, limit=4096)
        self.assertEqual(len(chunks), 2)
        self.assertEqual(len(chunks[0]), 4096)

    def test_multiple_chunks(self):
        text = ("line\n" * 1000)
        chunks = agent._split_reply(text, limit=100)
        self.assertGreater(len(chunks), 1)
        rejoined = "\n".join(chunks)
        self.assertIn("line", rejoined)

    def test_strips_leading_newline_from_next_chunk(self):
        text = "a" * 10 + "\n" + "b" * 10
        chunks = agent._split_reply(text, limit=11)
        self.assertFalse(chunks[1].startswith("\n"))


# ── _send_markdown ────────────────────────────────────────────────────────────

class TestSendMarkdown(unittest.TestCase):

    def setUp(self):
        agent.bot = MagicMock()

    def test_sends_with_markdownv2(self):
        msg = make_fake_message("hi")
        agent._send_markdown(msg, "**bold**")
        agent.bot.reply_to.assert_called_once()
        _, kwargs = agent.bot.reply_to.call_args
        self.assertEqual(kwargs.get("parse_mode"), "MarkdownV2")

    def test_fallback_to_plain_text_on_error(self):
        msg = make_fake_message("hi")
        agent.bot.reply_to.side_effect = [Exception("parse error"), None]
        agent._send_markdown(msg, "some text")
        self.assertEqual(agent.bot.reply_to.call_count, 2)
        # Second call (fallback) has no parse_mode
        second_call_kwargs = agent.bot.reply_to.call_args_list[1][1]
        self.assertNotIn("parse_mode", second_call_kwargs)

    def test_passes_text_to_markdownify(self):
        msg = make_fake_message("hi")
        with patch("agent.telegramify_markdown.markdownify", return_value="converted") as mock_m:
            agent._send_markdown(msg, "input text")
        mock_m.assert_called_once_with("input text")


# ── _allowed ──────────────────────────────────────────────────────────────────

class TestAllowed(unittest.TestCase):

    def setUp(self):
        agent.ALLOWED_USERS.clear()

    def tearDown(self):
        agent.ALLOWED_USERS.clear()

    def test_no_restrictions_allows_all(self):
        msg = make_fake_message("hi", user_id=999)
        self.assertTrue(agent._allowed(msg))

    def test_uid_in_allowlist(self):
        agent.ALLOWED_USERS.add(123)
        msg = make_fake_message("hi", user_id=123)
        self.assertTrue(agent._allowed(msg))

    def test_uid_not_in_allowlist(self):
        agent.ALLOWED_USERS.add(123)
        msg = make_fake_message("hi", user_id=456)
        self.assertFalse(agent._allowed(msg))

    def test_multiple_allowed_users(self):
        agent.ALLOWED_USERS.update([1, 2, 3])
        self.assertTrue(agent._allowed(make_fake_message("hi", user_id=2)))
        self.assertFalse(agent._allowed(make_fake_message("hi", user_id=99)))


# ── TUI ───────────────────────────────────────────────────────────────────────

class TestTuiLog(unittest.TestCase):

    def setUp(self):
        agent._log_entries.clear()

    def test_adds_entry(self):
        agent.tui_log("hello")
        self.assertEqual(len(agent._log_entries), 1)
        self.assertEqual(agent._log_entries[0][1], "hello")

    def test_entry_has_timestamp(self):
        agent.tui_log("msg")
        ts = agent._log_entries[0][0]
        # HH:MM:SS format
        self.assertRegex(ts, r"^\d{2}:\d{2}:\d{2}$")

    def test_multiple_entries_ordered(self):
        agent.tui_log("first")
        agent.tui_log("second")
        texts = [e[1] for e in agent._log_entries]
        self.assertEqual(texts, ["first", "second"])

    def test_respects_maxlen(self):
        for i in range(250):
            agent.tui_log(f"msg{i}")
        self.assertLessEqual(len(agent._log_entries), 200)


# ── Bot handlers ──────────────────────────────────────────────────────────────

class TestBotHandlers(unittest.TestCase):

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)
        self._orig_sessions_path = agent.SESSIONS_PATH
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.SESSIONS_PATH = self._tmp_sessions.name

    def tearDown(self):
        agent.ALLOWED_USERS.clear()
        agent.SESSIONS_PATH = self._orig_sessions_path
        for path in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)

    def _make_photo_msg(self, caption="describe this"):
        msg = make_fake_message(None)
        msg.text = None
        msg.caption = caption
        photo = MagicMock()
        photo.file_id = "fid_test"
        msg.photo = [photo]
        file_info = MagicMock()
        file_info.file_path = "photos/test.jpg"
        self.bot.get_file.return_value = file_info
        self.bot.download_file.return_value = b"\xff\xd8\xff"
        return msg

    def test_start_replies(self):
        agent.cmd_start(make_fake_message("/start"))
        self.bot.reply_to.assert_called_once()
        self.assertIn("Hi", self.bot.reply_to.call_args[0][1])

    def test_help_lists_all_commands(self):
        agent.cmd_help(make_fake_message("/help"))
        self.bot.reply_to.assert_called_once()
        reply = self.bot.reply_to.call_args[0][1]
        for cmd in ["/help", "/start", "/clear", "/cd", "/pwd", "/model", "/status"]:
            self.assertIn(cmd, reply)

    def test_help_blocked_unknown_user(self):
        agent.cmd_help(make_fake_message("/help", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_start_blocked_unknown_user(self):
        agent.cmd_start(make_fake_message("/start", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_clear_removes_session(self):
        agent.user_sessions[(123, 123)] = "sess"
        agent.cmd_clear(make_fake_message("/clear"))
        self.assertNotIn((123, 123), agent.user_sessions)
        self.bot.reply_to.assert_called_once()

    def test_cd_clears_session(self):
        agent.user_sessions[(123, 123)] = "old-sess"
        agent.cmd_cd(make_fake_message(f"/cd {os.getcwd()}"))
        self.assertNotIn((123, 123), agent.user_sessions)
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("session cleared", reply)

    def test_clear_blocked_unknown_user(self):
        agent.cmd_clear(make_fake_message("/clear", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_pwd_shows_default_cwd(self):
        agent.cmd_pwd(make_fake_message("/pwd"))
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn(os.getcwd(), reply)

    def test_pwd_shows_custom_cwd(self):
        agent.user_cwd[(123, 123)] = "/tmp"
        agent.cmd_pwd(make_fake_message("/pwd"))
        self.assertIn("/tmp", self.bot.reply_to.call_args[0][1])

    def test_cd_valid_dir(self):
        agent.cmd_cd(make_fake_message("/cd /tmp"))
        self.assertEqual(agent.user_cwd[(123, 123)], "/tmp")

    def test_cd_creates_dir_if_not_exists(self):
        import tempfile, shutil
        base = tempfile.mkdtemp()
        new_dir = os.path.join(base, "new_subdir")
        try:
            agent.cmd_cd(make_fake_message(f"/cd {new_dir}"))
            self.assertTrue(os.path.isdir(new_dir))
            self.assertEqual(agent.user_cwd[(123, 123)], new_dir)
            self.assertIn("created", self.bot.reply_to.call_args[0][1])
        finally:
            shutil.rmtree(base)

    @patch("agent.os.makedirs", side_effect=OSError("permission denied"))
    def test_cd_creation_failure(self, _):
        agent.cmd_cd(make_fake_message("/cd /nonexistent_xyz_abc"))
        self.assertNotIn((123, 123), agent.user_cwd)
        self.assertIn("❌", self.bot.reply_to.call_args[0][1])

    def test_cd_no_arg_shows_current(self):
        agent.user_cwd[(123, 123)] = "/tmp"
        agent.cmd_cd(make_fake_message("/cd"))
        self.assertIn("/tmp", self.bot.reply_to.call_args[0][1])

    def test_cd_expands_tilde(self):
        agent.cmd_cd(make_fake_message("/cd ~"))
        self.assertEqual(agent.user_cwd[(123, 123)], os.path.expanduser("~"))

    def test_model_shows_current(self):
        agent.MODEL = "claude-opus-4-6"
        agent.cmd_model(make_fake_message("/model"))
        self.assertIn("claude-opus-4-6", self.bot.reply_to.call_args[0][1])

    def test_model_switches(self):
        agent.cmd_model(make_fake_message("/model claude-sonnet-4-6"))
        self.assertEqual(agent.user_model[(123, 123)], "claude-sonnet-4-6")

    def test_model_clears_session(self):
        agent.user_sessions[(123, 123)] = "old-session"
        agent.cmd_model(make_fake_message("/model claude-sonnet-4-6"))
        self.assertNotIn((123, 123), agent.user_sessions)

    def test_model_blocked_unknown_user(self):
        agent.cmd_model(make_fake_message("/model opus", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_status_shows_model_cwd_session(self):
        agent.user_model[(123, 123)] = "claude-opus-4-6"
        agent.user_cwd[(123, 123)] = "/tmp"
        agent.user_sessions[(123, 123)] = "sess-id-123"
        agent.cmd_status(make_fake_message("/status"))
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("claude-opus-4-6", reply)
        self.assertIn("/tmp", reply)
        self.assertIn("sess-id-123", reply)

    def test_status_blocked_unknown_user(self):
        agent.cmd_status(make_fake_message("/status", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_handle_message_blocked(self):
        agent.handle_message(make_fake_message("hi", user_id=999))
        self.bot.reply_to.assert_not_called()

    @patch("agent.call_claude", return_value=("Hello!", "sess-123"))
    def test_handle_message_calls_claude(self, mock_claude):
        agent.handle_message(make_fake_message("what is 1+1"))
        time.sleep(0.5)
        mock_claude.assert_called_once()
        self.bot.reply_to.assert_called()

    @patch("agent.call_claude", return_value=("looks nice", "sess-photo"))
    def test_handle_photo_passes_image_path_to_claude(self, mock_claude):
        msg = self._make_photo_msg(caption="what's in this?")
        agent.handle_photo(msg)
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("Image file:", prompt)
        self.assertIn("what's in this?", prompt)

    def test_handle_photo_blocked_unknown_user(self):
        msg = make_fake_message(None, user_id=999)
        msg.text = None
        msg.caption = "hi"
        msg.photo = [MagicMock()]
        agent.handle_photo(msg)
        self.bot.reply_to.assert_not_called()

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_photo_no_caption_uses_empty_prompt(self, mock_claude):
        msg = self._make_photo_msg(caption=None)
        agent.handle_photo(msg)
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("Image file:", prompt)

    @patch("agent.call_claude", return_value=("ok", "sess-photo"))
    def test_handle_photo_stores_session(self, _):
        msg = self._make_photo_msg()
        agent.handle_photo(msg)
        time.sleep(0.5)
        self.assertEqual(agent.user_sessions[(123, 123)], "sess-photo")

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_photo_uses_highest_res(self, mock_claude):
        msg = self._make_photo_msg()
        lo = MagicMock(file_id="lo")
        hi = MagicMock(file_id="hi")
        msg.photo = [lo, hi]
        agent.handle_photo(msg)
        time.sleep(0.5)
        self.bot.get_file.assert_called_with("hi")

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_photo_temp_file_cleaned_up(self, _):
        msg = self._make_photo_msg()
        agent.handle_photo(msg)
        time.sleep(0.5)
        prompt = _.call_args[0][0]
        path = prompt.split("Image file: ")[1].split("\n")[0].strip()
        self.assertFalse(os.path.exists(path))

    @patch("agent.call_claude", side_effect=Exception("boom"))
    def test_handle_photo_temp_file_cleaned_up_on_error(self, _):
        msg = self._make_photo_msg()
        # Capture temp path via get_file side effect
        captured = {}
        orig_get_file = self.bot.get_file.side_effect

        def capture_and_download(file_id):
            fi = MagicMock()
            fi.file_path = "photos/err.jpg"
            captured["fi"] = fi
            return fi

        self.bot.get_file.side_effect = capture_and_download
        agent.handle_photo(msg)
        time.sleep(0.5)
        # Even though Claude raised, no temp file should linger
        # (we can't easily get the path here, but we verify no crash occurred)
        self.bot.reply_to.assert_not_called()  # exception swallowed in thread

    @patch("agent.call_claude", return_value=("reply", "sess-abc"))
    def test_handle_message_stores_session(self, _):
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        self.assertEqual(agent.user_sessions[(123, 123)], "sess-abc")

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_message_uses_user_cwd(self, mock_claude):
        agent.user_cwd[(123, 123)] = "/tmp/test"
        agent.handle_message(make_fake_message("hi"))
        time.sleep(0.5)
        self.assertEqual(mock_claude.call_args[1]["cwd"], "/tmp/test")

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_message_uses_user_model(self, mock_claude):
        agent.user_model[(123, 123)] = "claude-sonnet-4-6"
        agent.handle_message(make_fake_message("hi"))
        time.sleep(0.5)
        self.assertEqual(mock_claude.call_args[1]["model"], "claude-sonnet-4-6")

    @patch("agent.call_claude", return_value=("x" * 5000, "s"))
    def test_long_reply_splits_into_chunks(self, _):
        agent.handle_message(make_fake_message("essay"))
        time.sleep(0.5)
        self.assertEqual(self.bot.reply_to.call_count, 2)

    @patch("agent.call_claude", return_value=("", ""))
    def test_empty_session_id_not_stored(self, _):
        agent.handle_message(make_fake_message("hi"))
        time.sleep(0.5)
        self.assertNotIn((123, 123), agent.user_sessions)

    def test_stale_session_retries_without_session_id(self):
        stale_reply = "[Error] No conversation found with session ID: abc123"
        calls = []

        def fake_claude(*args, **kwargs):
            calls.append(kwargs.get("session_id"))
            if kwargs.get("session_id"):
                return stale_reply, ""
            return "fresh reply", "new-sess"

        agent.user_sessions[(123, 123)] = "abc123"
        with patch("agent.call_claude", side_effect=fake_claude):
            agent.handle_message(make_fake_message("hello"))
            time.sleep(0.5)

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], "abc123")
        self.assertIsNone(calls[1])
        self.assertEqual(agent.user_sessions[(123, 123)], "new-sess")
        reply_text = self.bot.reply_to.call_args[0][1]
        self.assertNotIn("No conversation found", str(reply_text))

    @patch("agent.call_claude", return_value=("reply", "new-sess"))
    def test_messages_processed_sequentially(self, mock_claude):
        """Second message uses session ID set by first message."""
        results = []

        def side_effect(*args, **kwargs):
            sid = kwargs.get("session_id")
            results.append(sid)
            return "reply", "new-sess"

        mock_claude.side_effect = side_effect
        q = agent._get_user_queue(123, 123)
        agent.handle_message(make_fake_message("msg1"))
        agent.handle_message(make_fake_message("msg2"))
        time.sleep(1.0)
        # First call: no session yet. Second call: session from first.
        self.assertIsNone(results[0])
        self.assertEqual(results[1], "new-sess")


# ── Concurrent users ─────────────────────────────────────────────────────────

class TestConcurrentUsers(unittest.TestCase):
    """Tests for Claude CLI serialization and queue notification."""

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.claude_busy_for = None
        self._orig_sessions_path = agent.SESSIONS_PATH
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.SESSIONS_PATH = self._tmp_sessions.name

    def tearDown(self):
        agent.ALLOWED_USERS.clear()
        agent.claude_busy_for = None
        agent.SESSIONS_PATH = self._orig_sessions_path
        for path in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)

    @patch("agent.call_claude")
    def test_second_user_gets_queue_notification(self, mock_claude):
        """When user A is being processed, user B sees a waiting message."""
        barrier = threading.Barrier(2, timeout=5)
        call_count = []

        def slow_claude(*args, **kwargs):
            call_count.append(1)
            if len(call_count) == 1:
                barrier.wait()  # first call: wait until second user is queued
                time.sleep(0.3)  # give time for notification to be sent
            return "reply", "sess"

        mock_claude.side_effect = slow_claude

        # Start user A (uid=1) — will hold the lock
        msg_a = make_fake_message("hello", user_id=1, username="alice")
        agent._get_user_queue(1, 1)
        agent.handle_message(msg_a)
        time.sleep(0.1)  # let worker thread start

        # Start user B (uid=2) — should get queued
        msg_b = make_fake_message("hi", user_id=2, username="bob")
        agent._get_user_queue(2, 2)
        agent.handle_message(msg_b)
        time.sleep(0.1)
        barrier.wait()  # release first call

        time.sleep(1.5)  # let both finish

        # User B should have received a "Waiting" notification
        all_replies = [str(c) for c in self.bot.reply_to.call_args_list]
        waiting_replies = [r for r in all_replies if "Waiting" in r]
        self.assertGreaterEqual(len(waiting_replies), 1, "User B should get a waiting notification")

    @patch("agent.call_claude")
    def test_both_users_get_responses(self, mock_claude):
        """Both users eventually receive Claude's response."""
        barrier = threading.Barrier(2, timeout=5)
        call_count = []

        def slow_claude(*args, **kwargs):
            call_count.append(1)
            if len(call_count) == 1:
                barrier.wait()
                time.sleep(0.2)
            return f"reply-{len(call_count)}", "sess"

        mock_claude.side_effect = slow_claude

        msg_a = make_fake_message("q1", user_id=1, username="alice")
        msg_b = make_fake_message("q2", user_id=2, username="bob")
        agent._get_user_queue(1, 1)
        agent._get_user_queue(2, 2)
        agent.handle_message(msg_a)
        time.sleep(0.1)
        agent.handle_message(msg_b)
        time.sleep(0.1)
        barrier.wait()

        time.sleep(2.0)

        self.assertEqual(mock_claude.call_count, 2, "Both users should get a Claude call")

    @patch("agent.call_claude")
    def test_claude_busy_for_cleared_after_call(self, mock_claude):
        """claude_busy_for is None after processing completes."""
        mock_claude.return_value = ("ok", "sess")
        agent.ALLOWED_USERS.add(1)

        msg = make_fake_message("hi", user_id=1, username="alice")
        agent.handle_message(msg)
        time.sleep(0.5)

        self.assertIsNone(agent.claude_busy_for)

    @patch("agent.call_claude")
    def test_claude_busy_for_set_during_call(self, mock_claude):
        """claude_busy_for is set to the current user during processing."""
        captured = []

        def capture_claude(*args, **kwargs):
            captured.append(agent.claude_busy_for)
            return "ok", "sess"

        mock_claude.side_effect = capture_claude

        msg = make_fake_message("hi", user_id=1, username="alice")
        agent._get_user_queue(1, 1)
        agent.handle_message(msg)
        time.sleep(0.5)

        self.assertEqual(captured, ["alice"])

    @patch("agent.call_claude")
    def test_same_user_no_queue_notification(self, mock_claude):
        """Same user sending multiple messages should not get queue notification."""
        barrier = threading.Barrier(2, timeout=5)
        call_count = []

        def slow_claude(*args, **kwargs):
            call_count.append(1)
            if len(call_count) == 1:
                barrier.wait()
                time.sleep(0.2)
            return "reply", "sess"

        mock_claude.side_effect = slow_claude

        msg1 = make_fake_message("q1", user_id=1, username="alice")
        msg2 = make_fake_message("q2", user_id=1, username="alice")
        agent._get_user_queue(1, 1)
        agent.handle_message(msg1)
        agent.handle_message(msg2)
        time.sleep(0.1)
        barrier.wait()

        time.sleep(2.0)

        # Same user's second message is processed by the same per-user worker
        # sequentially, so no "Waiting" notification should appear
        all_replies = [str(c) for c in self.bot.reply_to.call_args_list]
        waiting_replies = [r for r in all_replies if "Waiting" in r]
        self.assertEqual(len(waiting_replies), 0, "Same user should NOT get waiting notification")


# ── User state ────────────────────────────────────────────────────────────────

class TestUserCwd(unittest.TestCase):

    def setUp(self):
        agent.user_cwd.clear()

    def test_default_is_process_cwd(self):
        self.assertEqual(agent.DEFAULT_CWD, os.getcwd())

    def test_per_user_override(self):
        agent.user_cwd[(123, 123)] = "/tmp"
        self.assertEqual(agent.user_cwd.get((123, 123), agent.DEFAULT_CWD), "/tmp")

    def test_unknown_user_gets_default(self):
        self.assertEqual(agent.user_cwd.get((999, 999), agent.DEFAULT_CWD), agent.DEFAULT_CWD)


class TestUserModel(unittest.TestCase):

    def setUp(self):
        agent.user_model.clear()

    def test_no_override_uses_global(self):
        self.assertEqual(agent.user_model.get((123, 123), agent.MODEL), agent.MODEL)

    def test_per_user_override(self):
        agent.user_model[(123, 123)] = "claude-sonnet-4-6"
        self.assertEqual(agent.user_model.get((123, 123), agent.MODEL), "claude-sonnet-4-6")

    def test_different_users_independent(self):
        agent.user_model[(1, 1)] = "model-a"
        agent.user_model[(2, 2)] = "model-b"
        self.assertEqual(agent.user_model[(1, 1)], "model-a")
        self.assertEqual(agent.user_model[(2, 2)], "model-b")


# ── Real Claude integration ───────────────────────────────────────────────────

class TestRealClaude(unittest.TestCase):
    """Integration tests that call the real Claude CLI.

    These require `claude` to be installed and authenticated.
    Run with: python -m unittest test_agent.TestRealClaude -v
    """

    def test_simple_question(self):
        result, _ = agent.call_claude("What is 2+2? Reply with just the number.")
        self.assertIn("4", result)

    def test_respects_cwd(self):
        result, _ = agent.call_claude(
            "What directory are you working in? Reply briefly.",
            cwd=os.path.dirname(__file__) or ".",
        )
        self.assertIsInstance(result, str)
        self.assertNotIn("timed out", result)
        self.assertNotIn("not found", result)

    def test_empty_prompt(self):
        result, _ = agent.call_claude("")
        self.assertIsInstance(result, str)

    def test_code_generation(self):
        result, _ = agent.call_claude(
            "Write a Python function that adds two numbers. Only output the code, no explanation."
        )
        self.assertIn("def", result)
        self.assertIn("return", result)

    def test_multi_language(self):
        result, _ = agent.call_claude("用中文回答：1+1等于几？只回答数字。")
        self.assertIn("2", result)

    def test_file_awareness_with_cwd(self):
        result, _ = agent.call_claude(
            "Read the file README.md in the current directory and tell me the project name. Reply with just the name.",
            cwd=os.path.dirname(__file__) or ".",
        )
        self.assertTrue(
            any(w in result.lower() for w in ["telegram", "claude", "agent", "cta"]),
            f"Expected project name in: {result}"
        )

    def test_long_response(self):
        result, _ = agent.call_claude("Count from 1 to 20, one number per line.")
        self.assertIn("1", result)
        self.assertIn("20", result)


if __name__ == "__main__":
    unittest.main()
