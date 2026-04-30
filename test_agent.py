"""Tests for CTA (Claude Telegram Agent)."""

import json
import os
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from unittest.mock import patch, MagicMock, call

# Initialize with test config before importing
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import agent
import backends
import claude_code
import web

agent.init(agent.DEFAULT_CONFIG)


# ── Helpers ───────────────────────────────────────────────────────────────────

def make_fake_message(text, user_id=123, username="tester", chat_type="private", chat_title=None):
    msg = MagicMock()
    msg.text = text
    msg.caption = None
    msg.photo = None
    msg.document = None
    msg.voice = None
    msg.audio = None
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
    agent.user_timeout.clear()
    agent.chat_labels.clear()
    agent.msg_counts.clear()
    agent.last_reply.clear()
    return agent.bot


# ── Config ────────────────────────────────────────────────────────────────────

class TestConfig(unittest.TestCase):

    def test_default_config_keys(self):
        config = agent.DEFAULT_CONFIG
        self.assertEqual(config["telegram_bot_token"], "")
        self.assertEqual(config["allowed_users"], [])
        self.assertEqual(config["claude_timeout"], 1800)
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
        self.assertEqual(config["claude_timeout"], 1800)  # default preserved

    def test_missing_file_uses_defaults(self):
        with patch.object(agent, "CONFIG_PATH", "/nonexistent/config.json"):
            config = agent.load_config()
        self.assertEqual(config["claude_timeout"], 1800)

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
        self.assertTrue(agent.AGENTS_PATH.startswith(agent.CTA_HOME))
        self.assertTrue(agent.AGENTS_PATH.endswith("agents.json"))


# ── Session persistence ───────────────────────────────────────────────────────

class TestSessionPersistence(unittest.TestCase):

    def setUp(self):
        agent.user_sessions.clear()
        agent.last_active.clear()
        agent.chat_labels.clear()
        self.tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self.tmp.close()
        self._orig_sessions_path = agent.AGENTS_PATH
        agent.AGENTS_PATH = self.tmp.name

    def tearDown(self):
        agent.user_sessions.clear()
        agent.user_cwd.clear()
        agent.user_model.clear()
        agent.last_active.clear()
        agent.chat_labels.clear()
        agent.AGENTS_PATH = self._orig_sessions_path
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

    def test_model_persisted_and_restored(self):
        agent.user_sessions[(123, 123)] = "sess-abc"
        agent.user_model[(123, 123)] = "claude-haiku-4-5-20251001"
        agent.save_sessions()
        agent.user_sessions.clear()
        agent.user_model.clear()
        agent.load_sessions()
        self.assertEqual(agent.user_sessions[(123, 123)], "sess-abc")
        self.assertEqual(agent.user_model[(123, 123)], "claude-haiku-4-5-20251001")

    def test_model_only_persisted_without_session(self):
        agent.user_model[(99, 99)] = "claude-opus-4-6"
        agent.save_sessions()
        agent.user_model.clear()
        agent.load_sessions()
        self.assertEqual(agent.user_model[(99, 99)], "claude-opus-4-6")

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

    def test_label_roundtrip(self):
        """chat_labels should survive a save→load cycle so chat names persist
        across CTA restarts (without needing a Telegram message to repopulate)."""
        agent.chat_labels[(123, 456)] = "打印机"
        agent.save_sessions()
        agent.chat_labels.clear()
        agent.load_sessions()
        self.assertEqual(agent.chat_labels.get((123, 456)), "打印机")

    def test_pty_mode_only_chat_persists(self):
        """A chat with only `/pty on` (no session/cwd/model/label/last_active) must
        still get serialized so the toggle survives restart. Regression for the case
        where save_sessions' all_keys union excluded user_pty_mode."""
        agent.user_pty_mode[(789, 101)] = True
        try:
            agent.save_sessions()
            with open(self.tmp.name) as f:
                data = json.load(f)
            self.assertIn("789:101", data)
            self.assertTrue(data["789:101"].get("pty_mode"))
        finally:
            agent.user_pty_mode.pop((789, 101), None)

    def test_last_active_roundtrip(self):
        """last_active should be persisted to sessions.json and reloaded after restart."""
        agent.last_active[(123, 456)] = 1735000000.0
        agent.save_sessions()
        agent.last_active.clear()
        agent.load_sessions()
        self.assertEqual(agent.last_active.get((123, 456)), 1735000000.0)


# ── call_claude ───────────────────────────────────────────────────────────────

class TestCallClaude(unittest.TestCase):

    def _mock_proc(self, result="ok", session_id="sid", returncode=0):
        proc = MagicMock()
        proc.communicate.return_value = (
            json.dumps({"result": result, "session_id": session_id, "is_error": False}),
            "",
        )
        proc.returncode = returncode
        return proc

    def _mock_proc_error(self, stderr=""):
        proc = MagicMock()
        proc.communicate.return_value = ("", stderr)
        proc.returncode = 1
        return proc

    @patch("agent.subprocess.Popen")
    def test_appends_ctx_footer_with_context_window(self, mock_popen):
        """When modelUsage carries contextWindow, the footer should show
        'ctx: <input>/<window> (X%) / out: <output>'."""
        proc = MagicMock()
        proc.communicate.return_value = (
            json.dumps({
                "result": "Hi there",
                "session_id": "s",
                "is_error": False,
                "usage": {
                    "input_tokens": 3,
                    "cache_creation_input_tokens": 100,
                    "cache_read_input_tokens": 17000,
                    "output_tokens": 42,
                },
                "modelUsage": {
                    "claude-sonnet-4-6": {"contextWindow": 200000},
                },
            }),
            "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc
        text, _ = agent.call_claude("hi")
        self.assertIn("Hi there", text)
        self.assertIn("ctx: 17K/200K", text)
        self.assertIn("(9%)", text)  # 17103/200000 = 8.55% → rounds to 9%
        self.assertIn("out: 42", text)

    @patch("agent.subprocess.Popen")
    def test_output_uses_K_format_when_large(self, mock_popen):
        """Output >= 1000 should be formatted as e.g. '1.2K', not '1,242'."""
        proc = MagicMock()
        proc.communicate.return_value = (
            json.dumps({
                "result": "Big reply",
                "session_id": "s",
                "is_error": False,
                "usage": {
                    "input_tokens": 0,
                    "cache_read_input_tokens": 17000,
                    "output_tokens": 1242,
                },
                "modelUsage": {"claude-sonnet-4-6": {"contextWindow": 200000}},
            }),
            "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc
        text, _ = agent.call_claude("hi")
        self.assertIn("out: 1.2K", text)
        self.assertNotIn("out: 1,242", text)
        self.assertNotIn("out: 1242", text)

    @patch("agent.subprocess.Popen")
    def test_falls_back_to_in_out_when_context_window_missing(self, mock_popen):
        """If modelUsage.contextWindow is absent, fall back to the simple
        'in: X / out: Y' format rather than divide-by-zero."""
        proc = MagicMock()
        proc.communicate.return_value = (
            json.dumps({
                "result": "Hi",
                "session_id": "s",
                "is_error": False,
                "usage": {"input_tokens": 100, "output_tokens": 5},
            }),
            "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc
        text, _ = agent.call_claude("hi")
        self.assertIn("in: 100", text)
        self.assertIn("out: 5", text)
        self.assertNotIn("ctx:", text)

    @patch("agent.subprocess.Popen")
    def test_no_token_line_when_usage_missing(self, mock_popen):
        """If usage is missing (older claude versions, edge cases), don't append a
        misleading 'in: 0 / out: 0' line."""
        mock_popen.return_value = self._mock_proc("plain reply")
        text, _ = agent.call_claude("hi")
        self.assertEqual(text, "plain reply")
        self.assertNotIn("in: 0", text)

    @patch("agent.subprocess.Popen")
    def test_returns_text_and_session_id(self, mock_popen):
        mock_popen.return_value = self._mock_proc("Hello world", "abc-123")
        text, sid = agent.call_claude("hi")
        self.assertEqual(text, "Hello world")
        self.assertEqual(sid, "abc-123")

    @patch("agent.subprocess.Popen")
    def test_passes_cwd(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", cwd="/tmp/test")
        self.assertEqual(mock_popen.call_args[1]["cwd"], "/tmp/test")

    @patch("agent.subprocess.Popen")
    def test_uses_default_cwd_when_none(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi")
        self.assertEqual(mock_popen.call_args[1]["cwd"], agent.DEFAULT_CWD)

    @patch("agent.subprocess.Popen")
    def test_uses_global_model(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi")
        args = mock_popen.call_args[0][0]
        self.assertIn("--model", args)
        self.assertIn(agent.MODEL, args)

    @patch("agent.subprocess.Popen")
    def test_uses_override_model(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", model="claude-sonnet-4-6")
        args = mock_popen.call_args[0][0]
        idx = args.index("--model")
        self.assertEqual(args[idx + 1], "claude-sonnet-4-6")

    @patch("agent.subprocess.Popen")
    def test_includes_required_flags(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi")
        args = mock_popen.call_args[0][0]
        self.assertIn("--print", args)
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--output-format", args)
        self.assertIn("json", args)

    @patch("agent.subprocess.Popen")
    @patch("agent.tui_log")
    def test_logs_send_to_claude(self, mock_log, mock_popen):
        """Each subprocess launch should log a '→ claude' line so wait/run timing
        is visible separately from message receipt (HANDLE_MSG)."""
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", uid=123, chat_id=456, model="claude-opus-4-7")
        log_lines = [args[0][0] for args in mock_log.call_args_list]
        self.assertTrue(any("→ claude" in line for line in log_lines))
        self.assertTrue(any("claude-opus-4-7" in line for line in log_lines))

    @patch("agent.subprocess.Popen")
    def test_sets_cta_uid_chat_id_env(self, mock_popen):
        """When uid/chat_id are passed, subprocess env must include CTA_UID/CTA_CHAT_ID
        so cron.py and other helpers know which chat they're in."""
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", uid=2018384667, chat_id=-5112107804)
        env = mock_popen.call_args[1]["env"]
        self.assertEqual(env["CTA_UID"], "2018384667")
        self.assertEqual(env["CTA_CHAT_ID"], "-5112107804")

    @patch("agent.subprocess.Popen")
    def test_env_preserves_parent_env(self, mock_popen):
        """CTA_UID/CTA_CHAT_ID must be added without wiping out PATH etc."""
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", uid=1, chat_id=2)
        env = mock_popen.call_args[1]["env"]
        self.assertIn("PATH", env)

    @patch("agent.subprocess.Popen")
    def test_no_cta_env_when_uid_missing(self, mock_popen):
        """Without uid/chat_id, don't add CTA_UID/CTA_CHAT_ID to subprocess env."""
        mock_popen.return_value = self._mock_proc()
        # Scrub parent env so the assertion isn't influenced by whether the
        # test is itself running under an agent.py that already set these.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CTA_UID", None)
            os.environ.pop("CTA_CHAT_ID", None)
            agent.call_claude("hi")
        env = mock_popen.call_args[1]["env"]
        self.assertNotIn("CTA_UID", env)
        self.assertNotIn("CTA_CHAT_ID", env)

    @patch("agent.subprocess.Popen")
    def test_resumes_session(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi", session_id="sess-xyz")
        args = mock_popen.call_args[0][0]
        self.assertIn("--resume", args)
        self.assertIn("sess-xyz", args)

    @patch("agent.subprocess.Popen")
    def test_no_resume_without_session_id(self, mock_popen):
        mock_popen.return_value = self._mock_proc()
        agent.call_claude("hi")
        args = mock_popen.call_args[0][0]
        self.assertNotIn("--resume", args)

    @patch("agent.subprocess.Popen")
    def test_stderr_returned_on_empty_stdout(self, mock_popen):
        mock_popen.return_value = self._mock_proc_error("something broke")
        text, sid = agent.call_claude("hi")
        self.assertIn("Error", text)
        self.assertIn("something broke", text)
        self.assertEqual(sid, "")

    @patch("agent.subprocess.Popen")
    def test_empty_stdout_and_stderr(self, mock_popen):
        mock_popen.return_value = self._mock_proc_error("")
        text, _ = agent.call_claude("hi")
        self.assertEqual(text, "(empty response)")

    @patch("agent.subprocess.Popen")
    def test_strips_whitespace(self, mock_popen):
        mock_popen.return_value = self._mock_proc("  hello  \n")
        text, _ = agent.call_claude("hi")
        self.assertEqual(text, "hello")

    @patch("agent.subprocess.Popen")
    def test_timeout(self, mock_popen):
        proc = MagicMock()
        proc.communicate.side_effect = [subprocess.TimeoutExpired("claude", 600), ("", "")]
        mock_popen.return_value = proc
        text, sid = agent.call_claude("hi")
        self.assertIn("timed out", text)
        self.assertEqual(sid, "")

    @patch("agent.subprocess.Popen", side_effect=FileNotFoundError)
    def test_cli_not_found(self, _):
        text, _ = agent.call_claude("hi")
        self.assertIn("not found", text)

    @patch("agent.subprocess.Popen")
    def test_returns_error_on_failure(self, mock_popen):
        mock_popen.return_value = self._mock_proc_error("transient error")
        text, sid = agent.call_claude("hi")
        self.assertIn("transient error", text)
        self.assertEqual(sid, "")
        self.assertEqual(mock_popen.call_count, 1)

    @patch("agent.subprocess.Popen", side_effect=FileNotFoundError)
    def test_no_retry_on_file_not_found(self, mock_popen):
        text, _ = agent.call_claude("hi")
        self.assertIn("not found", text)
        self.assertEqual(mock_popen.call_count, 1)


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
        web._log_entries.clear()

    def test_adds_entry(self):
        agent.tui_log("hello")
        self.assertEqual(len(web._log_entries), 1)
        self.assertEqual(web._log_entries[0][1], "hello")

    def test_entry_has_timestamp(self):
        agent.tui_log("msg")
        ts = web._log_entries[0][0]
        # HH:MM:SS format
        self.assertRegex(ts, r"^\d{2}:\d{2}:\d{2}$")

    def test_multiple_entries_ordered(self):
        agent.tui_log("first")
        agent.tui_log("second")
        texts = [e[1] for e in web._log_entries]
        self.assertEqual(texts, ["first", "second"])

    def test_respects_maxlen(self):
        for i in range(250):
            agent.tui_log(f"msg{i}")
        self.assertLessEqual(len(web._log_entries), 200)


# ── Bot handlers ──────────────────────────────────────────────────────────────

class TestBotHandlers(unittest.TestCase):

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)
        self._orig_sessions_path = agent.AGENTS_PATH
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.AGENTS_PATH = self._tmp_sessions.name
        self._orig_memory_dir = agent.MEMORY_DIR
        self._tmp_memory_dir = tempfile.mkdtemp()
        agent.MEMORY_DIR = self._tmp_memory_dir
        self._orig_preamble_dir = agent.PREAMBLE_DIR
        self._tmp_preamble_dir = tempfile.mkdtemp()
        agent.PREAMBLE_DIR = self._tmp_preamble_dir

    def tearDown(self):
        import shutil
        agent.ALLOWED_USERS.clear()
        agent.AGENTS_PATH = self._orig_sessions_path
        agent.MEMORY_DIR = self._orig_memory_dir
        agent.PREAMBLE_DIR = self._orig_preamble_dir
        for path in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)
        shutil.rmtree(self._tmp_memory_dir, ignore_errors=True)
        shutil.rmtree(self._tmp_preamble_dir, ignore_errors=True)
        # Stop any backends that tests left behind so reader threads / PTYs
        # don't leak across tests.
        for key, b in list(agent._backends.items()):
            try:
                b.stop()
            except Exception:
                pass
            agent._backends.pop(key, None)

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

    def test_pty_status_default_off(self):
        agent.user_pty_mode.pop((123, 123), None)
        agent.cmd_pty(make_fake_message("/pty"))
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("off", reply)

    def test_pty_on_sets_flag(self):
        agent.user_pty_mode.pop((123, 123), None)
        agent.cmd_pty(make_fake_message("/pty on"))
        self.assertTrue(agent.user_pty_mode.get((123, 123)))
        self.assertIn("on", self.bot.reply_to.call_args[0][1])

    def test_pty_off_clears_flag_and_stops_instance(self):
        agent.user_pty_mode[(123, 123)] = True
        fake_backend = MagicMock()
        agent._backends[(123, 123)] = fake_backend
        agent.cmd_pty(make_fake_message("/pty off"))
        self.assertNotIn((123, 123), agent.user_pty_mode)
        self.assertNotIn((123, 123), agent._backends)
        fake_backend.stop.assert_called_once()

    def test_pty_invalid_arg_shows_usage(self):
        agent.cmd_pty(make_fake_message("/pty wat"))
        self.assertIn("Usage", self.bot.reply_to.call_args[0][1])

    def test_pty_blocked_unknown_user(self):
        agent.cmd_pty(make_fake_message("/pty on", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_clear_stops_backend(self):
        fake_backend = MagicMock()
        agent._backends[(123, 123)] = fake_backend
        agent.cmd_clear(make_fake_message("/clear"))
        fake_backend.stop.assert_called_once()
        self.assertNotIn((123, 123), agent._backends)

    def test_model_change_stops_backend(self):
        fake_backend = MagicMock()
        agent._backends[(123, 123)] = fake_backend
        agent.cmd_model(make_fake_message("/model claude-opus-4-6"))
        fake_backend.stop.assert_called_once()
        self.assertNotIn((123, 123), agent._backends)

    @patch("backends.pty.claude_code.ClaudeCode")
    def test_pty_backend_passes_cta_env(self, mock_cc_class):
        """ClaudeCode should be constructed with CTA_UID/CTA_CHAT_ID in extra_env so
        cron.py / notify.py invoked from inside the PTY chat can identify themselves.
        Without this, those scripts exit unless --uid/--chat-id flags are passed."""
        mock_instance = MagicMock()
        mock_instance.proc = None  # reader will exit immediately
        mock_cc_class.return_value = mock_instance
        b = backends.PtyBackend(123, 456)
        try:
            b._ensure_started()
        finally:
            b.stop()
        kwargs = mock_cc_class.call_args[1]
        self.assertIn("extra_env", kwargs)
        self.assertEqual(kwargs["extra_env"]["CTA_UID"], "123")
        self.assertEqual(kwargs["extra_env"]["CTA_CHAT_ID"], "456")

    @patch("backends.pty.claude_code.ClaudeCode")
    def test_pty_backend_cleans_up_when_start_raises(self, mock_cc_class):
        """If cc.start() raises (e.g. ClaudeNotReady), the half-started instance
        must be stopped so we don't leak subprocess + master_fd, and the backend
        must not register the cc (so the next attempt spawns fresh)."""
        mock_instance = MagicMock()
        mock_instance.proc = None
        mock_instance.start.side_effect = claude_code.ClaudeNotReady("setup failed")
        mock_cc_class.return_value = mock_instance
        b = backends.PtyBackend(123, 456)
        with self.assertRaises(claude_code.ClaudeNotReady):
            b._ensure_started()
        mock_instance.stop.assert_called_once()
        self.assertIsNone(b.cc)

    @patch("backends.pty.claude_code.ClaudeCode")
    def test_pty_backend_stops_dead_cc_before_respawn(self, mock_cc_class):
        """If the cached ClaudeCode has died (proc.poll() != None), the old instance
        must be stopped before spawning a replacement — otherwise the dead PTY's
        master_fd leaks."""
        dead_cc = MagicMock()
        dead_cc.proc.poll.return_value = 1  # process exited
        fresh_cc = MagicMock(proc=None)
        mock_cc_class.return_value = fresh_cc
        b = backends.PtyBackend(123, 456)
        b._cc = dead_cc
        b._stop_event = threading.Event()  # so _teardown can set it
        try:
            b._ensure_started()
        finally:
            b.stop()
        dead_cc.stop.assert_called_once()

    @patch("agent._get_backend")
    @patch("agent.call_claude")
    def test_pty_mode_dispatch_calls_backend_send(self, mock_print, mock_get):
        """PTY mode dispatch must go through the backend's send (not call_claude).
        The long-lived reader inside the backend surfaces the response."""
        backend = MagicMock(spec=backends.PtyBackend)
        mock_get.return_value = backend
        agent.user_pty_mode[(123, 123)] = True
        try:
            agent.handle_message(make_fake_message("hello"))
            time.sleep(0.5)
            mock_get.assert_called_once_with((123, 123))
            backend.send.assert_called_once()
            self.assertIn("hello", backend.send.call_args[0][0])
            mock_print.assert_not_called()
        finally:
            agent.user_pty_mode.pop((123, 123), None)

    def test_cd_clears_session(self):
        agent.user_sessions[(123, 123)] = "old-sess"
        agent.cmd_cd(make_fake_message(f"/cd {os.getcwd()}"))
        self.assertNotIn((123, 123), agent.user_sessions)
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("session cleared", reply)

    def test_cd_stops_backend(self):
        """Like /clear and /model, /cd must stop the backend so the next message
        respawns with the new cwd. Otherwise a stale PTY keeps running in the old dir."""
        fake_backend = MagicMock()
        agent._backends[(123, 123)] = fake_backend
        agent.cmd_cd(make_fake_message(f"/cd {os.getcwd()}"))
        fake_backend.stop.assert_called_once()
        self.assertNotIn((123, 123), agent._backends)

    def test_pty_backend_reader_forwards_chunks_via_callback(self):
        """The reader loop forwards each batch of new lines through on_output.
        Validates the streaming pipeline without spinning a real PTY."""
        cc = MagicMock()
        cc.proc.poll.return_value = None
        chunks = iter([["hello"], ["second batch"], ["third"]])
        stop_event = threading.Event()
        def fake_read(timeout=0.5):
            try:
                return next(chunks)
            except StopIteration:
                stop_event.set()
                return []
        cc.read_new_output.side_effect = fake_read
        b = backends.PtyBackend(123, 123)
        b._cc = cc
        b._stop_event = stop_event
        sent: list[str] = []
        b.on_output = lambda text: sent.append(text)
        b._reader_loop()
        self.assertEqual(sent, ["hello", "second batch", "third"])

    def test_pty_backend_reader_exits_when_proc_dies(self):
        """If cc.proc has exited (poll() != None), the reader must break out
        before the stop event fires, so a crashed claude doesn't leave a
        thread spinning on a dead PTY."""
        cc = MagicMock()
        cc.proc.poll.return_value = 0  # exited
        b = backends.PtyBackend(123, 123)
        b._cc = cc
        b._stop_event = threading.Event()
        b._reader_loop()
        cc.read_new_output.assert_not_called()

    def test_pty_backend_reader_exits_on_stop_event(self):
        cc = MagicMock()
        cc.proc.poll.return_value = None
        cc.read_new_output.side_effect = lambda timeout=0.5: []
        b = backends.PtyBackend(123, 123)
        b._cc = cc
        b._stop_event = threading.Event()
        b._stop_event.set()
        b._reader_loop()
        cc.read_new_output.assert_not_called()

    def test_pty_backend_stop_signals_reader_and_joins(self):
        """PtyBackend.stop must set the stop event, join the reader thread,
        and then call cc.stop. Otherwise the reader could read from a closed fd."""
        cc = MagicMock()
        ev = threading.Event()
        joined = threading.Event()
        def runner():
            ev.wait(timeout=2)
            joined.set()
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        b = backends.PtyBackend(123, 123)
        b._cc = cc
        b._stop_event = ev
        b._reader = t
        b.stop()
        self.assertTrue(joined.is_set())
        self.assertIsNone(b.cc)
        cc.stop.assert_called_once()

    def test_clear_blocked_unknown_user(self):
        agent.cmd_clear(make_fake_message("/clear", user_id=999))
        self.bot.reply_to.assert_not_called()

    @patch("agent.subprocess.Popen")
    def test_call_claude_releases_local_semaphore_after_global_swap(self, mock_popen):
        """Codex P1 regression: if max_concurrent_claude is changed at runtime
        between acquire and release, the release must hit the *acquired*
        semaphore, not the new one — otherwise permits leak on the old and
        inflate on the new."""
        proc = MagicMock()
        proc.communicate.return_value = (
            json.dumps({"result": "hi", "session_id": "s", "is_error": False}),
            "",
        )
        proc.returncode = 0
        mock_popen.return_value = proc

        original_sem = agent._claude_semaphore
        # Mid-call, swap to a new semaphore (simulates /config POST). We do this
        # by replacing the global right before the second acquire would happen.
        try:
            agent.call_claude("hi", uid=1, chat_id=2)
            # Replace global like _web_set_config would do.
            agent._claude_semaphore = threading.Semaphore(5)
            agent.call_claude("hi", uid=1, chat_id=2)
            # The original semaphore must still have its single permit
            # available — otherwise the local-ref capture failed.
            self.assertTrue(original_sem.acquire(blocking=False))
            original_sem.release()
        finally:
            agent._claude_semaphore = original_sem

    def test_cancel_marks_key_when_no_subprocess_yet(self):
        """Codex P2: /cancel on a chat that's blocked at the semaphore (worker
        in claude_active_keys but no subprocess yet) must add to _cancelled_keys
        so call_claude bails out when the slot opens."""
        key = (123, 123)
        agent.claude_active_keys.add(key)
        agent._cancelled_keys.discard(key)
        try:
            agent.cmd_cancel(make_fake_message("/cancel"))
            self.assertIn(key, agent._cancelled_keys)
            reply = self.bot.reply_to.call_args[0][1]
            self.assertIn("queued", reply.lower())
        finally:
            agent.claude_active_keys.discard(key)
            agent._cancelled_keys.discard(key)

    @patch("agent.subprocess.Popen")
    def test_call_claude_bails_when_cancelled_before_acquire(self, mock_popen):
        """If /cancel marked the key while we were blocked at the semaphore,
        call_claude must NOT spawn the subprocess once the slot opens."""
        agent._cancelled_keys.add((1, 2))
        try:
            text, sid = agent.call_claude("hi", uid=1, chat_id=2)
            self.assertEqual(text, "(cancelled)")
            self.assertEqual(sid, "")
            mock_popen.assert_not_called()
        finally:
            agent._cancelled_keys.discard((1, 2))

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

    def test_model_preserves_session(self):
        agent.user_sessions[(123, 123)] = "old-session"
        agent.cmd_model(make_fake_message("/model claude-sonnet-4-6"))
        self.assertEqual(agent.user_sessions[(123, 123)], "old-session")

    def test_model_blocked_unknown_user(self):
        agent.cmd_model(make_fake_message("/model opus", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_timeout_shows_current(self):
        agent.TIMEOUT = 600
        agent.cmd_timeout(make_fake_message("/timeout"))
        self.assertIn("600", self.bot.reply_to.call_args[0][1])

    def test_timeout_sets_value(self):
        agent.cmd_timeout(make_fake_message("/timeout 120"))
        self.assertEqual(agent.user_timeout[(123, 123)], 120)

    def test_timeout_reset(self):
        agent.user_timeout[(123, 123)] = 120
        agent.cmd_timeout(make_fake_message("/timeout reset"))
        self.assertNotIn((123, 123), agent.user_timeout)

    def test_timeout_invalid_value(self):
        agent.cmd_timeout(make_fake_message("/timeout abc"))
        self.assertIn("❌", self.bot.reply_to.call_args[0][1])

    def test_timeout_blocked_unknown_user(self):
        agent.cmd_timeout(make_fake_message("/timeout 60", user_id=999))
        self.bot.reply_to.assert_not_called()

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_timeout_used_in_call_claude(self, mock_claude):
        agent.user_timeout[(123, 123)] = 999
        agent.handle_message(make_fake_message("hi"))
        time.sleep(0.5)
        self.assertEqual(mock_claude.call_args[1]["timeout"], 999)

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

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_prompt_includes_memory_path(self, mock_claude):
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("memory:", prompt)
        self.assertIn("123:123.md", prompt)

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_memory_dir_created_on_load(self, _):
        self.assertTrue(os.path.isdir(agent.MEMORY_DIR))

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_custom_preamble_injected(self, mock_claude):
        preamble_path = os.path.join(agent.PREAMBLE_DIR, "123:123.md")
        with open(preamble_path, "w") as f:
            f.write("Always reply in Chinese.")
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("Always reply in Chinese.", prompt)

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_no_preamble_file_no_injection(self, mock_claude):
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        # system preamble present, no extra blank sections
        self.assertIn("Agent chat:", prompt)
        self.assertNotIn("Always reply in Chinese.", prompt)

    def _make_document_msg(self, filename="image.png", mime_type="image/png", caption="check this"):
        msg = make_fake_message(None)
        msg.text = None
        msg.caption = caption
        doc = MagicMock()
        doc.file_id = "doc_fid"
        doc.file_name = filename
        doc.mime_type = mime_type
        msg.document = doc
        file_info = MagicMock()
        file_info.file_path = f"documents/{filename}"
        self.bot.get_file.return_value = file_info
        self.bot.download_file.return_value = b"\x89PNG\r\n"
        return msg

    @patch("agent.call_claude", return_value=("looks good", "sess-doc"))
    def test_handle_document_passes_file_to_claude(self, mock_claude):
        msg = self._make_document_msg()
        agent.handle_document(msg)
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("Read tool", prompt)
        self.assertIn("check this", prompt)

    def test_handle_document_blocked_unknown_user(self):
        msg = self._make_document_msg()
        msg.from_user.id = 999
        msg.chat.id = 999
        agent.handle_document(msg)
        self.bot.reply_to.assert_not_called()

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_handle_document_temp_file_cleaned_up(self, _):
        msg = self._make_document_msg()
        agent.handle_document(msg)
        time.sleep(0.5)
        prompt = _.call_args[0][0]
        path = prompt.split("analyze the file at: ")[1].split("\n")[0].strip()
        self.assertFalse(os.path.exists(path))

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
        barrier = threading.Barrier(2, timeout=5)

        def side_effect(*args, **kwargs):
            sid = kwargs.get("session_id")
            results.append(sid)
            # Hold msg1 until msg2 is queued *after* worker has started msg1,
            # so the two messages are NOT batched together.
            if len(results) == 1:
                barrier.wait()
                time.sleep(0.1)
            return "reply", "new-sess"

        mock_claude.side_effect = side_effect
        agent._get_user_queue(123, 123)
        agent.handle_message(make_fake_message("msg1"))
        time.sleep(0.1)   # let the worker start processing msg1
        barrier.wait()    # now msg1 is in flight — queue msg2 while it's running
        agent.handle_message(make_fake_message("msg2"))

        time.sleep(1.5)
        # First call: no session yet. Second call: session from first.
        self.assertIsNone(results[0])
        self.assertEqual(results[1], "new-sess")


# ── shutdown handler ─────────────────────────────────────────────────────────

class TestShutdownHandler(unittest.TestCase):
    """Tests for _kill_tracked_subprocs — must touch only CTA-spawned PIDs."""

    def setUp(self):
        agent._current_procs.clear()
        agent._backends.clear()

    def tearDown(self):
        agent._current_procs.clear()
        agent._backends.clear()

    @patch("agent.os.killpg")
    @patch("agent.os.getpgid", return_value=12345)
    def test_kills_tracked_print_subprocs(self, mock_getpgid, mock_killpg):
        proc = MagicMock()
        proc.pid = 9999
        agent._current_procs[(1, 2)] = proc
        n = agent._kill_tracked_subprocs()
        self.assertEqual(n, 1)
        mock_killpg.assert_called_once_with(12345, agent.signal.SIGKILL)
        self.assertNotIn((1, 2), agent._current_procs)

    def test_stops_tracked_backends(self):
        b = MagicMock()
        agent._backends[(1, 2)] = b
        n = agent._kill_tracked_subprocs()
        self.assertEqual(n, 1)
        b.stop.assert_called_once()
        self.assertNotIn((1, 2), agent._backends)

    @patch("agent.os.killpg", side_effect=ProcessLookupError)
    @patch("agent.os.getpgid", return_value=12345)
    def test_falls_back_to_proc_kill_when_killpg_fails(self, mock_getpgid, mock_killpg):
        """If the process group is already gone, fall back to proc.kill() so
        we don't crash the shutdown handler."""
        proc = MagicMock()
        proc.pid = 9999
        agent._current_procs[(1, 2)] = proc
        n = agent._kill_tracked_subprocs()
        self.assertEqual(n, 1)
        proc.kill.assert_called_once()

    def test_only_touches_tracked_pids(self):
        """Sanity: a foreign PID NOT in _current_procs / _backends must
        never be killed. Empty tracking dicts → zero kills."""
        with patch("agent.os.killpg") as mock_killpg, \
             patch("agent.os.getpgid"):
            n = agent._kill_tracked_subprocs()
        self.assertEqual(n, 0)
        mock_killpg.assert_not_called()


# ── /cancel command ──────────────────────────────────────────────────────────

class TestCancel(unittest.TestCase):

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)
        agent._cancelled_keys.clear()
        agent._current_procs.clear()

    def tearDown(self):
        agent.ALLOWED_USERS.clear()
        agent._cancelled_keys.clear()
        agent._current_procs.clear()
        agent.claude_active_keys.discard((123, 123))
        with agent.user_queues_lock:
            agent.user_queues.pop((123, 123), None)

    def test_nothing_to_cancel(self):
        """No active task and empty queue → "Nothing to cancel"."""
        agent.cmd_cancel(make_fake_message("/cancel"))
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("Nothing to cancel", reply)

    def test_blocked_unknown_user(self):
        agent.cmd_cancel(make_fake_message("/cancel", user_id=999))
        self.bot.reply_to.assert_not_called()

    def test_kills_active_proc_for_this_chat(self):
        """When Claude is running for this chat, the proc is killed via the backend."""
        proc = MagicMock()
        agent._current_procs[(123, 123)] = proc
        agent._backends[(123, 123)] = backends.PrintBackend(123, 123)
        agent.cmd_cancel(make_fake_message("/cancel"))
        proc.kill.assert_called_once()
        self.assertIn((123, 123), agent._cancelled_keys)
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("stopped", reply)

    def test_does_not_kill_proc_for_other_chat(self):
        """Active task belongs to a different chat — don't kill it."""
        proc = MagicMock()
        agent._current_procs[(999, 999)] = proc
        agent._backends[(999, 999)] = backends.PrintBackend(999, 999)
        agent.cmd_cancel(make_fake_message("/cancel"))
        proc.kill.assert_not_called()
        self.assertNotIn((123, 123), agent._cancelled_keys)

    def test_cancel_kills_correct_proc_when_concurrent(self):
        """With two chats active concurrently, cancel only kills the right one."""
        proc_a = MagicMock()
        proc_b = MagicMock()
        agent._current_procs[(123, 123)] = proc_a
        agent._current_procs[(456, 456)] = proc_b
        agent._backends[(123, 123)] = backends.PrintBackend(123, 123)
        agent._backends[(456, 456)] = backends.PrintBackend(456, 456)
        agent.cmd_cancel(make_fake_message("/cancel"))  # cancels chat (123,123)
        proc_a.kill.assert_called_once()
        proc_b.kill.assert_not_called()

    def test_drains_pending_queue(self):
        """Pending messages in the queue are removed."""
        # Inject a queue directly without starting a worker thread
        q = queue.Queue()
        q.put(make_fake_message("a"))
        q.put(make_fake_message("b"))
        with agent.user_queues_lock:
            agent.user_queues[(123, 123)] = q
        agent.cmd_cancel(make_fake_message("/cancel"))
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("pending", reply)
        self.assertEqual(q.qsize(), 0)

    def test_cancelled_key_suppresses_reply(self):
        """_cancelled_keys prevents _process_message from sending a reply."""
        agent._cancelled_keys.add((123, 123))
        # Simulate what _process_message does after call_claude returns
        key = (123, 123)
        if key in agent._cancelled_keys:
            agent._cancelled_keys.discard(key)
            # No reply sent — key was consumed
        self.assertNotIn(key, agent._cancelled_keys)
        self.bot.reply_to.assert_not_called()


# ── Concurrent users ─────────────────────────────────────────────────────────

class TestConcurrentUsers(unittest.TestCase):
    """Tests for Claude CLI per-chat serialization and cross-chat concurrency."""

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.claude_active_keys.clear()
        self._orig_sessions_path = agent.AGENTS_PATH
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.AGENTS_PATH = self._tmp_sessions.name

    def tearDown(self):
        agent.ALLOWED_USERS.clear()
        agent.claude_active_keys.clear()
        agent.AGENTS_PATH = self._orig_sessions_path
        for path in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)

    @patch("agent.call_claude")
    def test_different_chats_run_concurrently(self, mock_claude):
        """Different chats run their Claude calls concurrently (no global lock)."""
        started = []
        barrier = threading.Barrier(2, timeout=5)

        def slow_claude(*args, **kwargs):
            started.append(1)
            if len(started) == 1:
                barrier.wait()  # wait for second call to also start
            return "reply", "sess"

        mock_claude.side_effect = slow_claude

        msg_a = make_fake_message("hello", user_id=1, username="alice")
        msg_b = make_fake_message("hi", user_id=2, username="bob")
        agent._get_user_queue(1, 1)
        agent._get_user_queue(2, 2)
        agent.handle_message(msg_a)
        time.sleep(0.1)
        agent.handle_message(msg_b)

        # Both calls start concurrently — barrier.wait() would deadlock if serialized
        barrier.wait()
        time.sleep(1.0)
        self.assertEqual(mock_claude.call_count, 2)

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
    def test_active_keys_cleared_after_call(self, mock_claude):
        """claude_active_keys is empty after processing completes."""
        mock_claude.return_value = ("ok", "sess")
        agent.ALLOWED_USERS.add(1)

        msg = make_fake_message("hi", user_id=1, username="alice")
        agent.handle_message(msg)
        time.sleep(0.5)

        self.assertNotIn((1, 1), agent.claude_active_keys)

    @patch("agent.call_claude")
    def test_active_keys_set_during_call(self, mock_claude):
        """claude_active_keys contains the chat key during processing."""
        captured = []

        def capture_claude(*args, **kwargs):
            captured.append((1, 1) in agent.claude_active_keys)
            return "ok", "sess"

        mock_claude.side_effect = capture_claude

        msg = make_fake_message("hi", user_id=1, username="alice")
        agent._get_user_queue(1, 1)
        agent.handle_message(msg)
        time.sleep(0.5)

        self.assertEqual(captured, [True])

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


# ── Message batching ──────────────────────────────────────────────────────────

class TestMessageBatching(unittest.TestCase):
    """Tests for batching multiple pending text messages into one Claude call."""

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.claude_active_keys.clear()
        self._orig_sessions_path = agent.AGENTS_PATH
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.AGENTS_PATH = self._tmp_sessions.name

    def tearDown(self):
        agent.ALLOWED_USERS.clear()
        agent.claude_active_keys.clear()
        agent.AGENTS_PATH = self._orig_sessions_path
        for path in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(path):
                os.unlink(path)

    def test_is_plain_text_true(self):
        msg = make_fake_message("hello", user_id=1, username="alice")
        self.assertTrue(agent._is_plain_text(msg))

    def test_is_plain_text_false_no_text(self):
        msg = make_fake_message("hello", user_id=1, username="alice")
        msg.text = None
        self.assertFalse(agent._is_plain_text(msg))

    def test_is_plain_text_false_dict(self):
        self.assertFalse(agent._is_plain_text({"_type": "cron"}))

    def test_is_plain_text_false_has_document(self):
        msg = make_fake_message("hello", user_id=1, username="alice")
        msg.document = object()
        self.assertFalse(agent._is_plain_text(msg))

    def test_is_plain_text_false_has_voice(self):
        msg = make_fake_message("hello", user_id=1, username="alice")
        msg.voice = object()
        self.assertFalse(agent._is_plain_text(msg))

    @patch("agent.call_claude")
    def test_multiple_texts_batched_into_one_call(self, mock_claude):
        """When multiple text messages queue up, they are combined into one Claude call."""
        barrier = threading.Barrier(2, timeout=5)
        captured_prompts = []

        def slow_claude(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            # First call: hold until second+third messages are queued
            if len(captured_prompts) == 1:
                barrier.wait()
                time.sleep(0.1)
            return "reply", "sess"

        mock_claude.side_effect = slow_claude

        msg1 = make_fake_message("first", user_id=1, username="alice")
        msg2 = make_fake_message("second", user_id=1, username="alice")
        msg3 = make_fake_message("third", user_id=1, username="alice")

        agent._get_user_queue(1, 1)
        agent.handle_message(msg1)
        time.sleep(0.1)  # let worker pick up msg1
        # Queue msg2 and msg3 while msg1 is being processed
        agent.handle_message(msg2)
        agent.handle_message(msg3)
        barrier.wait()  # release msg1 processing

        time.sleep(1.5)  # let batched call complete

        # Should be exactly 2 Claude calls: one for msg1, one for msg2+msg3 batched
        self.assertEqual(mock_claude.call_count, 2)
        batched_prompt = captured_prompts[1]
        self.assertIn("[Message 1]: second", batched_prompt)
        self.assertIn("[Message 2]: third", batched_prompt)

    @patch("agent.call_claude")
    def test_non_text_interrupts_batch(self, mock_claude):
        """A non-text message in the queue stops batching; msg2 is not merged into msg1."""
        captured_prompts = []
        barrier = threading.Barrier(2, timeout=5)

        def slow_claude(prompt, *args, **kwargs):
            captured_prompts.append(prompt)
            if len(captured_prompts) == 1:
                barrier.wait()
                time.sleep(0.1)
            return "reply", "sess"

        mock_claude.side_effect = slow_claude

        msg1 = make_fake_message("first", user_id=1, username="alice")
        msg2 = make_fake_message("second", user_id=1, username="alice")
        msg_doc = make_fake_message("caption", user_id=1, username="alice")
        msg_doc.document = MagicMock()

        agent._get_user_queue(1, 1)
        agent.handle_message(msg1)
        time.sleep(0.1)
        # Queue: doc (non-batchable) then msg2 — should not be batched with msg1
        agent.handle_message(msg_doc)
        agent.handle_message(msg2)
        barrier.wait()

        time.sleep(2.0)

        # msg1 is processed alone (doc stops the drain); doc processing fails (mock can't
        # write MagicMock bytes to a tempfile) → no call_claude; msg2 gets its own call.
        # Total: 2 calls (msg1, msg2).
        self.assertEqual(mock_claude.call_count, 2)
        # msg1 prompt is just its text — no batching prefix
        self.assertNotIn("[Message", captured_prompts[0])
        # msg2 prompt is just its text — processed alone, not merged with anything
        self.assertNotIn("[Message", captured_prompts[1])


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


# ── Polling loop ─────────────────────────────────────────────────────────────

class TestPollingLoop(unittest.TestCase):
    """Tests for _polling_loop, the wrapper around bot.infinity_polling that
    reconnects on transient network errors (Errno 49 etc.)."""

    def test_retries_on_connection_error(self):
        """A transient exception from infinity_polling should not kill the loop —
        it should sleep and retry."""
        calls = []

        def fake_polling():
            calls.append(1)
            if len(calls) >= 3:
                raise SystemExit  # bypasses 'except Exception' to break the loop
            raise ConnectionError(f"transient #{len(calls)}")

        fake_bot = MagicMock()
        fake_bot.infinity_polling = fake_polling
        with patch("agent.bot", fake_bot), patch("agent.time.sleep") as mock_sleep:
            with self.assertRaises(SystemExit):
                agent._polling_loop()
        self.assertEqual(len(calls), 3, "loop should have retried 3 times before SystemExit")
        # Two retries between three attempts → two sleep(10) calls
        self.assertEqual(mock_sleep.call_args_list, [call(10), call(10)])

    def test_does_not_swallow_keyboardinterrupt(self):
        """KeyboardInterrupt / SystemExit must propagate so shutdown works."""
        fake_bot = MagicMock()
        fake_bot.infinity_polling = MagicMock(side_effect=KeyboardInterrupt)
        with patch("agent.bot", fake_bot), patch("agent.time.sleep"):
            with self.assertRaises(KeyboardInterrupt):
                agent._polling_loop()


# ── Cron scheduler ───────────────────────────────────────────────────────────

class TestCronScheduler(unittest.TestCase):

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)
        self._orig_crons_dir = agent.CRONS_DIR
        self._orig_memory_dir = agent.MEMORY_DIR
        self._orig_sessions_path = agent.AGENTS_PATH
        self._tmp_crons = tempfile.mkdtemp()
        self._tmp_memory = tempfile.mkdtemp()
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.CRONS_DIR = self._tmp_crons
        agent.MEMORY_DIR = self._tmp_memory
        agent.AGENTS_PATH = self._tmp_sessions.name

    def tearDown(self):
        import shutil
        agent.CRONS_DIR = self._orig_crons_dir
        agent.MEMORY_DIR = self._orig_memory_dir
        agent.AGENTS_PATH = self._orig_sessions_path
        shutil.rmtree(self._tmp_crons, ignore_errors=True)
        shutil.rmtree(self._tmp_memory, ignore_errors=True)
        for p in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(p):
                os.unlink(p)

    def _write_cron_file(self, uid, chat_id, jobs):
        path = os.path.join(self._tmp_crons, f"{uid}:{chat_id}.json")
        with open(path, "w") as f:
            json.dump(jobs, f)

    def test_load_cron_jobs_missing_file(self):
        jobs = agent._load_cron_jobs(999, 999)
        self.assertEqual(jobs, [])

    def test_save_and_load_cron_jobs(self):
        jobs = [{"id": "a1b2", "schedule": "0 9 * * *", "prompt": "test", "next_run": "2026-04-08T09:00:00"}]
        agent._save_cron_jobs(123, 456, jobs)
        loaded = agent._load_cron_jobs(123, 456)
        self.assertEqual(loaded[0]["id"], "a1b2")

    @patch("agent.call_claude", return_value=("cron result", "sess-cron"))
    def test_process_cron_sends_message(self, mock_claude):
        done = threading.Event()
        task = {"_type": "cron", "uid": 123, "chat_id": 123, "job_id": "x1", "prompt": "do something"}
        agent._process_cron(123, 123, task, done)
        self.bot.send_message.assert_called()
        args = self.bot.send_message.call_args[0]
        self.assertEqual(args[0], 123)  # chat_id

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_process_cron_prompt_includes_preamble(self, mock_claude):
        done = threading.Event()
        task = {"_type": "cron", "uid": 123, "chat_id": 456, "job_id": "ab12", "prompt": "my task"}
        agent._process_cron(123, 456, task, done)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("Agent chat:", prompt)
        self.assertIn("123:456", prompt)
        self.assertIn("my task", prompt)

    @patch("agent.call_claude", return_value=("result", "sess"))
    def test_cron_scheduler_fires_due_jobs(self, mock_claude):
        past = "2000-01-01T00:00:00"
        jobs = [{"id": "j1", "schedule": "0 9 * * *", "prompt": "hello", "next_run": past}]
        self._write_cron_file(123, 456, jobs)
        # Run one scheduler tick manually
        from croniter import croniter
        from datetime import datetime
        now = datetime.now()
        changed = False
        for fname in os.listdir(agent.CRONS_DIR):
            if not fname.endswith(".json"):
                continue
            uid_str, chat_str = fname[:-5].split(":", 1)
            uid, chat_id = int(uid_str), int(chat_str)
            loaded = agent._load_cron_jobs(uid, chat_id)
            for job in loaded:
                next_run = datetime.fromisoformat(job["next_run"])
                if next_run <= now:
                    agent._get_user_queue(uid, chat_id).put({
                        "_type": "cron", "uid": uid, "chat_id": chat_id,
                        "job_id": job["id"], "prompt": job["prompt"],
                    })
                    cron = croniter(job["schedule"], now)
                    job["next_run"] = cron.get_next(datetime).isoformat()
                    changed = True
            if changed:
                agent._save_cron_jobs(uid, chat_id, loaded)
        time.sleep(0.5)
        mock_claude.assert_called()
        # next_run should be advanced
        reloaded = agent._load_cron_jobs(123, 456)
        self.assertGreater(datetime.fromisoformat(reloaded[0]["next_run"]), now)

    def test_cron_tick_defaults_missing_next_run(self):
        """Jobs without next_run (e.g. written directly to JSON) get auto-defaulted to the next
        scheduled occurrence and persisted — they should NOT fire immediately."""
        from datetime import datetime, timedelta
        # Daily at 09:00. Regardless of 'now', next occurrence is in the future.
        jobs = [{"id": "no-nr", "schedule": "0 9 * * *", "prompt": "hi"}]
        self._write_cron_file(123, 456, jobs)
        before = len(agent._get_user_queue(123, 456).queue) if hasattr(agent._get_user_queue(123, 456), "queue") else 0
        agent._cron_tick_once(datetime.now())
        reloaded = agent._load_cron_jobs(123, 456)
        self.assertIn("next_run", reloaded[0])
        next_run = datetime.fromisoformat(reloaded[0]["next_run"])
        self.assertGreater(next_run, datetime.now() - timedelta(seconds=1))
        # Nothing should have been queued — the default fills next_run for the future, not 'now'.
        q = agent._get_user_queue(123, 456)
        self.assertTrue(q.empty())

    def test_crons_parse_error_detection(self):
        """Broken JSON should be surfaced via _crons_parse_error, not silently hidden."""
        path = os.path.join(self._tmp_crons, "123:456.json")
        with open(path, "w") as f:
            f.write('[{"id": "x", "prompt": "bad "unescaped" quotes"}]')
        err = agent._crons_parse_error(123, 456)
        self.assertIsNotNone(err)
        self.assertIn("line", err)
        # Valid file returns None
        agent._save_cron_jobs(123, 456, [{"id": "ok", "schedule": "0 9 * * *", "prompt": "fine"}])
        self.assertIsNone(agent._crons_parse_error(123, 456))
        # Missing file returns None
        self.assertIsNone(agent._crons_parse_error(999, 999))

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_broken_cron_file_surfaces_warning_in_preamble(self, mock_claude):
        """When the cron file is invalid JSON, the next agent turn should see a warning."""
        path = os.path.join(self._tmp_crons, "123:123.json")
        with open(path, "w") as f:
            f.write('[{"id": "x", "prompt": "bad "quotes"}]')
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("INVALID JSON", prompt)
        self.assertIn("123:123.json", prompt)

    def test_cron_tick_defaults_invalid_next_run(self):
        """Unparseable next_run values should also be defaulted, not crash the scheduler."""
        from datetime import datetime
        jobs = [{"id": "bad-nr", "schedule": "0 9 * * *", "prompt": "hi", "next_run": "not-a-date"}]
        self._write_cron_file(123, 456, jobs)
        agent._cron_tick_once(datetime.now())
        reloaded = agent._load_cron_jobs(123, 456)
        datetime.fromisoformat(reloaded[0]["next_run"])  # must now parse

    @patch("agent.call_claude", return_value=("ok", "s"))
    def test_prompt_includes_crons_path(self, mock_claude):
        """Regular messages should reference the crons file path."""
        agent.handle_message(make_fake_message("hello"))
        time.sleep(0.5)
        prompt = mock_claude.call_args[0][0]
        self.assertIn("crons:", prompt)
        self.assertIn(".json", prompt)


# ── Web API ──────────────────────────────────────────────────────────────────

class TestWebAPI(unittest.TestCase):
    """Tests for Flask web dashboard API endpoints."""

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)
        self._orig_crons_dir = agent.CRONS_DIR
        self._orig_sessions_path = agent.AGENTS_PATH
        self._tmp_crons = tempfile.mkdtemp()
        self._tmp_sessions = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        self._tmp_sessions.close()
        agent.CRONS_DIR = self._tmp_crons
        agent.AGENTS_PATH = self._tmp_sessions.name
        self.client = agent.app.test_client()

    def tearDown(self):
        import shutil
        agent.CRONS_DIR = self._orig_crons_dir
        agent.AGENTS_PATH = self._orig_sessions_path
        shutil.rmtree(self._tmp_crons, ignore_errors=True)
        for p in [self._tmp_sessions.name, self._tmp_sessions.name + ".tmp"]:
            if os.path.exists(p):
                os.unlink(p)

    def _write_cron_file(self, uid, chat_id, jobs):
        path = os.path.join(self._tmp_crons, f"{uid}:{chat_id}.json")
        with open(path, "w") as f:
            json.dump(jobs, f)

    def _write_sessions(self, data):
        with open(self._tmp_sessions.name, "w") as f:
            json.dump(data, f)

    # ── DELETE /chat/<uid>/<chat_id> ──

    def _setup_purge_dirs(self):
        """Redirect MEMORY_DIR / PREAMBLE_DIR to tempdirs so _purge_chat is hermetic."""
        import shutil
        self._tmp_memory = tempfile.mkdtemp()
        self._tmp_preamble = tempfile.mkdtemp()
        self._orig_memory = agent.MEMORY_DIR
        self._orig_preamble = agent.PREAMBLE_DIR
        agent.MEMORY_DIR = self._tmp_memory
        agent.PREAMBLE_DIR = self._tmp_preamble
        self.addCleanup(lambda: setattr(agent, "MEMORY_DIR", self._orig_memory))
        self.addCleanup(lambda: setattr(agent, "PREAMBLE_DIR", self._orig_preamble))
        self.addCleanup(lambda: shutil.rmtree(self._tmp_memory, ignore_errors=True))
        self.addCleanup(lambda: shutil.rmtree(self._tmp_preamble, ignore_errors=True))

    def test_purge_chat_clears_in_memory_state(self):
        self._setup_purge_dirs()
        key = (789, 1011)
        agent.user_sessions[key] = "sess-x"
        agent.user_cwd[key] = "/tmp"
        agent.user_model[key] = "claude-opus-4-7"
        agent.msg_counts[key] = 5
        agent.last_reply[key] = "hi"
        agent.last_active[key] = 1735000000.0
        agent.chat_labels[key] = "DM:tester"
        agent.chat_history[key] = [{"role": "user", "text": "x", "ts": 1.0}]

        agent._purge_chat(*key)

        for d_name in ("user_sessions", "user_cwd", "user_model", "msg_counts",
                       "last_reply", "last_active", "chat_labels", "chat_history"):
            self.assertNotIn(key, getattr(agent, d_name), f"{d_name} not cleared")

    def test_purge_chat_deletes_files(self):
        self._setup_purge_dirs()
        uid, chat_id = 789, 1011
        memory_path = os.path.join(self._tmp_memory, f"{uid}:{chat_id}.md")
        crons_path = os.path.join(self._tmp_crons, f"{uid}:{chat_id}.json")
        preamble_path = os.path.join(self._tmp_preamble, f"{uid}:{chat_id}.md")
        for p, content in [(memory_path, "remember me"),
                           (crons_path, "[]"),
                           (preamble_path, "be nice")]:
            with open(p, "w") as f:
                f.write(content)

        agent._purge_chat(uid, chat_id)

        for p in (memory_path, crons_path, preamble_path):
            self.assertFalse(os.path.exists(p), f"{p} still exists")

    def test_purge_chat_idempotent_when_files_missing(self):
        """Should not raise if files don't exist (e.g. fresh chat with no memory/cron yet)."""
        self._setup_purge_dirs()
        agent._purge_chat(404, 404)  # never had any state — must not error

    def test_delete_chat_endpoint(self):
        self._setup_purge_dirs()
        key = (789, 1011)
        agent.user_sessions[key] = "sess-x"
        agent.msg_counts[key] = 1
        r = self.client.delete("/chat/789/1011")
        self.assertEqual(r.status_code, 200)
        self.assertNotIn(key, agent.user_sessions)
        self.assertNotIn(key, agent.msg_counts)

    def test_delete_chat_refuses_when_active(self):
        self._setup_purge_dirs()
        key = (789, 1011)
        agent.user_sessions[key] = "sess-x"
        agent.claude_active_keys.add(key)
        try:
            r = self.client.delete("/chat/789/1011")
            self.assertEqual(r.status_code, 409)
            self.assertIn(key, agent.user_sessions, "must NOT delete state when chat is active")
        finally:
            agent.claude_active_keys.discard(key)

    def test_status_includes_last_active(self):
        """/status response should include last_active per session for the chats UI."""
        agent.last_active.clear()
        agent.last_active[(123, 456)] = 1735000000.0
        agent.msg_counts[(123, 456)] = 1
        r = self.client.get("/status")
        d = r.get_json()
        sess = next((s for s in d["sessions"] if s["uid"] == 123 and s["chat_id"] == 456), None)
        self.assertIsNotNone(sess)
        self.assertEqual(sess["last_active"], 1735000000.0)
        agent.last_active.clear()
        agent.msg_counts.pop((123, 456), None)

    def test_chat_push_updates_last_active(self):
        """Sending or receiving a message should bump last_active."""
        agent.last_active.clear()
        before = time.time()
        agent._chat_push(123, 456, "user", "hi")
        self.assertIn((123, 456), agent.last_active)
        self.assertGreaterEqual(agent.last_active[(123, 456)], before)
        agent.last_active.clear()

    @patch("agent.call_claude", return_value=("hi back", "sess-new"))
    def test_assistant_last_active_persists_to_disk(self, mock_claude):
        """Regression for PR #73 codex P2: last_active for the assistant message
        must be persisted (save_sessions runs after _chat_push, not before)."""
        # Redirect AGENTS_PATH to a temp file
        tmp = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp.close()
        orig = agent.AGENTS_PATH
        agent.AGENTS_PATH = tmp.name
        try:
            agent.last_active.clear()
            agent.handle_message(make_fake_message("hello"))
            time.sleep(0.5)
            # last_active should now be in sessions.json on disk
            with open(tmp.name) as f:
                data = json.load(f)
            entries_with_active = [e for e in data.values() if "last_active" in e]
            self.assertTrue(entries_with_active,
                            f"expected last_active in saved sessions; got {data}")
        finally:
            agent.AGENTS_PATH = orig
            agent.last_active.clear()
            os.unlink(tmp.name)

    # ── GET /config ──

    def test_config_returns_system_preamble_template(self):
        """/config response should include the hardcoded system preamble with placeholders,
        so the web UI can display it above Global preamble."""
        r = self.client.get("/config")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn("system_preamble", d)
        sp = d["system_preamble"]
        self.assertIn("<uid>", sp)
        self.assertIn("<chat_id>", sp)
        self.assertIn("Always reply after tool use", sp)
        self.assertIn("cron.py", sp)
        self.assertIn("Telegram MCP", sp)

    # ── GET /status ──

    def test_status_returns_sessions(self):
        agent.msg_counts[(123, 456)] = 5
        r = self.client.get("/status")
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertIn("sessions", d)
        self.assertIn("model", d)

    # ── GET /cronjobs ──

    def test_cronjobs_empty(self):
        r = self.client.get("/cronjobs")
        d = r.get_json()
        self.assertEqual(d["jobs"], [])

    def test_cronjobs_lists_jobs(self):
        self._write_cron_file(123, 456, [
            {"id": "test1", "schedule": "0 9 * * *", "prompt": "hello", "next_run": "2026-04-10T09:00:00"},
        ])
        r = self.client.get("/cronjobs")
        d = r.get_json()
        self.assertEqual(len(d["jobs"]), 1)
        self.assertEqual(d["jobs"][0]["id"], "test1")
        self.assertEqual(d["jobs"][0]["schedule"], "0 9 * * *")

    def test_cronjobs_skips_example_with_far_future_next_run(self):
        self._write_cron_file(123, 456, [
            {"id": "example", "schedule": "0 9 * * *", "prompt": "hi", "next_run": "2099-01-01T09:00:00"},
        ])
        r = self.client.get("/cronjobs")
        d = r.get_json()
        self.assertEqual(d["jobs"], [])

    # ── POST /cronjobs ──

    def test_create_cronjob(self):
        r = self.client.post("/cronjobs", json={
            "uid": 123, "chat_id": 456, "id": "morning",
            "schedule": "0 9 * * *", "prompt": "good morning",
        })
        self.assertEqual(r.status_code, 200)
        d = r.get_json()
        self.assertTrue(d["ok"])
        self.assertIn("next_run", d)
        # Verify persisted
        jobs = agent._load_cron_jobs(123, 456)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "morning")

    def test_create_cronjob_removes_example(self):
        self._write_cron_file(123, 456, [
            {"id": "example", "schedule": "0 9 * * *", "prompt": "hi", "next_run": "2099-01-01T09:00:00"},
        ])
        r = self.client.post("/cronjobs", json={
            "uid": 123, "chat_id": 456, "id": "real",
            "schedule": "30 8 * * *", "prompt": "wake up",
        })
        self.assertEqual(r.status_code, 200)
        jobs = agent._load_cron_jobs(123, 456)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "real")

    def test_create_cronjob_missing_fields(self):
        r = self.client.post("/cronjobs", json={"uid": 123})
        self.assertEqual(r.status_code, 400)
        self.assertIn("error", r.get_json())

    def test_create_cronjob_invalid_schedule(self):
        r = self.client.post("/cronjobs", json={
            "uid": 123, "chat_id": 456, "id": "bad",
            "schedule": "not-a-cron", "prompt": "test",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid cron", r.get_json()["error"])

    def test_create_cronjob_duplicate_id(self):
        self._write_cron_file(123, 456, [
            {"id": "dup", "schedule": "0 9 * * *", "prompt": "a", "next_run": "2026-04-10T09:00:00"},
        ])
        r = self.client.post("/cronjobs", json={
            "uid": 123, "chat_id": 456, "id": "dup",
            "schedule": "0 10 * * *", "prompt": "b",
        })
        self.assertEqual(r.status_code, 409)

    def test_create_cronjob_invalid_uid(self):
        r = self.client.post("/cronjobs", json={
            "uid": "abc", "chat_id": 456, "id": "x",
            "schedule": "0 9 * * *", "prompt": "test",
        })
        self.assertEqual(r.status_code, 400)
        self.assertIn("Invalid uid", r.get_json()["error"])

    # ── DELETE /cronjobs ──

    def test_delete_cronjob(self):
        self._write_cron_file(123, 456, [
            {"id": "del-me", "schedule": "0 9 * * *", "prompt": "x", "next_run": "2026-04-10T09:00:00"},
            {"id": "keep", "schedule": "0 10 * * *", "prompt": "y", "next_run": "2026-04-10T10:00:00"},
        ])
        r = self.client.delete("/cronjobs/123/456/del-me")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["ok"])
        jobs = agent._load_cron_jobs(123, 456)
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["id"], "keep")

    def test_delete_cronjob_not_found(self):
        self._write_cron_file(123, 456, [])
        r = self.client.delete("/cronjobs/123/456/nonexistent")
        self.assertEqual(r.status_code, 404)

    # ── GET /chats ──

    def test_chats_empty(self):
        r = self.client.get("/chats")
        d = r.get_json()
        self.assertEqual(d["chats"], [])

    def test_chats_returns_sessions(self):
        self._write_sessions({
            "123:456": {"session": "s1", "cwd": "/tmp/project"},
            "123:789": {"cwd": "/tmp/other"},
        })
        r = self.client.get("/chats")
        d = r.get_json()
        self.assertEqual(len(d["chats"]), 2)
        uids = {c["uid"] for c in d["chats"]}
        self.assertEqual(uids, {123})
        cwds = {c["cwd"] for c in d["chats"]}
        self.assertIn("/tmp/project", cwds)

    def test_chats_uses_labels(self):
        self._write_sessions({"123:456": {"cwd": "/tmp"}})
        agent.chat_labels[(123, 456)] = "DM:tester"
        r = self.client.get("/chats")
        d = r.get_json()
        self.assertEqual(d["chats"][0]["label"], "DM:tester")

    # ── GET / ──

    def test_index_returns_html(self):
        r = self.client.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn(b"<!DOCTYPE html>", r.data)


if __name__ == "__main__":
    unittest.main()
