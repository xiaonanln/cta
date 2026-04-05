"""Tests for CTA (Claude Telegram Agent)."""

import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

# Initialize with test config before importing
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import agent

agent.init(agent.DEFAULT_CONFIG)


def make_fake_message(text, user_id=123, username="tester"):
    """Create a fake Telegram message object."""
    msg = MagicMock()
    msg.text = text
    msg.from_user.id = user_id
    msg.from_user.username = username
    msg.chat.id = user_id
    msg.message_id = 1
    return msg


def setup_fake_bot():
    """Create agent with a fake bot that doesn't hit Telegram API."""
    agent.bot = MagicMock()
    agent.user_sessions.clear()
    agent.user_cwd.clear()
    return agent.bot


class TestCallClaude(unittest.TestCase):
    """Tests for the call_claude function."""

    @patch("agent.subprocess.run")
    def test_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "Hello world", "session_id": "abc-123", "is_error": false}',
            stderr=""
        )
        result = agent.call_claude("hi")
        self.assertEqual(result[0], "Hello world")
        self.assertEqual(result[1], "abc-123")

    @patch("agent.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "ok", "session_id": "s1", "is_error": false}',
            stderr=""
        )
        agent.call_claude("hi", cwd="/tmp/test")
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["cwd"], "/tmp/test")

    @patch("agent.subprocess.run")
    def test_uses_dangerously_skip_permissions(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "ok", "session_id": "s1", "is_error": false}',
            stderr=""
        )
        agent.call_claude("hi")
        args = mock_run.call_args[0][0]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--print", args)
        self.assertIn("--output-format", args)
        self.assertIn("json", args)

    @patch("agent.subprocess.run")
    def test_returns_stderr_on_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="some error")
        result = agent.call_claude("hi")
        self.assertIn("Error", result[0])
        self.assertIn("some error", result[0])

    @patch("agent.subprocess.run")
    def test_returns_empty_response_message(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="")
        result = agent.call_claude("hi")
        self.assertEqual(result[0], "(empty response)")

    @patch("agent.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120))
    def test_timeout_returns_message(self, mock_run):
        result = agent.call_claude("hi")
        self.assertIn("timed out", result[0])

    @patch("agent.subprocess.run", side_effect=FileNotFoundError)
    def test_cli_not_found(self, mock_run):
        result = agent.call_claude("hi")
        self.assertIn("not found", result[0])

    @patch("agent.subprocess.run")
    def test_strips_whitespace(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "  hello  \\n", "session_id": "s1", "is_error": false}',
            stderr=""
        )
        result = agent.call_claude("hi")
        self.assertEqual(result[0], "hello")

    @patch("agent.subprocess.run")
    def test_resumes_session(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "ok", "session_id": "sess-456", "is_error": false}',
            stderr=""
        )
        agent.call_claude("hi", session_id="sess-456")
        args = mock_run.call_args[0][0]
        self.assertIn("--resume", args)
        self.assertIn("sess-456", args)

    @patch("agent.subprocess.run")
    def test_returns_session_id(self, mock_run):
        mock_run.return_value = MagicMock(
            stdout='{"result": "hello", "session_id": "new-session-789", "is_error": false}',
            stderr=""
        )
        text, session_id = agent.call_claude("hi")
        self.assertEqual(session_id, "new-session-789")


class TestSessionManagement(unittest.TestCase):
    """Tests for Claude session management."""

    def setUp(self):
        agent.user_sessions.clear()
        agent.user_cwd.clear()

    def test_sessions_start_empty(self):
        self.assertEqual(len(agent.user_sessions), 0)

    def test_clear_removes_session(self):
        agent.user_sessions[123] = "sess-id"
        agent.user_sessions.pop(123, None)
        self.assertNotIn(123, agent.user_sessions)


class TestUserCwd(unittest.TestCase):
    """Tests for working directory management."""

    def setUp(self):
        agent.user_cwd.clear()

    def test_default_cwd_is_current_dir(self):
        self.assertEqual(agent.DEFAULT_CWD, os.getcwd())

    def test_user_cwd_override(self):
        agent.user_cwd[123] = "/tmp"
        self.assertEqual(agent.user_cwd.get(123, agent.DEFAULT_CWD), "/tmp")

    def test_user_without_cwd_gets_default(self):
        self.assertEqual(agent.user_cwd.get(999, agent.DEFAULT_CWD), agent.DEFAULT_CWD)


class TestAllowedUsers(unittest.TestCase):
    """Tests for user whitelist."""

    def test_empty_allows_all(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": ""}):
            users = set(
                int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
            )
            self.assertEqual(users, set())

    def test_parses_comma_separated(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": "123,456,789"}):
            users = set(
                int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
            )
            self.assertEqual(users, {123, 456, 789})

    def test_single_user(self):
        with patch.dict(os.environ, {"ALLOWED_USERS": "2018384667"}):
            users = set(
                int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
            )
            self.assertEqual(users, {2018384667})


class TestBotHandlers(unittest.TestCase):
    """Tests using a fake bot to verify handler behavior."""

    def setUp(self):
        self.bot = setup_fake_bot()
        agent.ALLOWED_USERS.clear()
        agent.ALLOWED_USERS.add(123)

    def test_start_command(self):
        msg = make_fake_message("/start")
        agent.cmd_start(msg)
        self.bot.reply_to.assert_called_once()
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("Hi", reply)

    def test_start_blocked_for_unknown_user(self):
        msg = make_fake_message("/start", user_id=999)
        agent.cmd_start(msg)
        self.bot.reply_to.assert_not_called()

    def test_clear_command(self):
        agent.user_sessions[123] = "some-session-id"
        msg = make_fake_message("/clear")
        agent.cmd_clear(msg)
        self.assertNotIn(123, agent.user_sessions)
        self.bot.reply_to.assert_called_once()

    def test_pwd_command(self):
        msg = make_fake_message("/pwd")
        agent.cmd_pwd(msg)
        self.bot.reply_to.assert_called_once()
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn(os.getcwd(), reply)

    def test_cd_to_valid_dir(self):
        msg = make_fake_message("/cd /tmp")
        agent.cmd_cd(msg)
        self.assertEqual(agent.user_cwd[123], "/tmp")
        self.bot.reply_to.assert_called_once()

    def test_cd_to_invalid_dir(self):
        msg = make_fake_message("/cd /nonexistent_dir_xyz")
        agent.cmd_cd(msg)
        self.assertNotIn(123, agent.user_cwd)
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("❌", reply)

    def test_cd_no_arg_shows_current(self):
        agent.user_cwd[123] = "/tmp"
        msg = make_fake_message("/cd")
        agent.cmd_cd(msg)
        reply = self.bot.reply_to.call_args[0][1]
        self.assertIn("/tmp", reply)

    @patch("agent.call_claude", return_value=("Hello from Claude!", "sess-123"))
    def test_message_calls_claude_and_replies(self, mock_claude):
        msg = make_fake_message("what is 1+1")
        agent.handle_message(msg)
        # handle_message runs in a thread, wait briefly
        import time
        time.sleep(0.5)
        mock_claude.assert_called_once()
        self.bot.reply_to.assert_called()
        reply = self.bot.reply_to.call_args[0][1]
        self.assertEqual(reply, "Hello from Claude!")

    @patch("agent.call_claude", return_value=("Hello from Claude!", "sess-123"))
    def test_message_stores_session_id(self, mock_claude):
        msg = make_fake_message("hello")
        agent.handle_message(msg)
        import time
        time.sleep(0.5)
        self.assertEqual(agent.user_sessions[123], "sess-123")

    @patch("agent.call_claude", return_value=("ok", "sess-abc"))
    def test_message_uses_user_cwd(self, mock_claude):
        agent.user_cwd[123] = "/tmp/test"
        msg = make_fake_message("hi")
        agent.handle_message(msg)
        import time
        time.sleep(0.5)
        _, kwargs = mock_claude.call_args
        self.assertEqual(kwargs["cwd"], "/tmp/test")

    def test_message_blocked_for_unknown_user(self):
        msg = make_fake_message("hi", user_id=999)
        agent.handle_message(msg)
        self.bot.reply_to.assert_not_called()

    @patch("agent.call_claude", return_value=("x" * 5000, "sess-123"))
    def test_long_reply_split(self, mock_claude):
        msg = make_fake_message("write a long essay")
        agent.handle_message(msg)
        import time
        time.sleep(0.5)
        # Should be called twice (5000 / 4096 = 2 chunks)
        self.assertEqual(self.bot.reply_to.call_count, 2)


class TestConfig(unittest.TestCase):
    """Tests for config loading."""

    def test_default_config(self):
        with patch.dict(os.environ, {}, clear=True):
            config = agent.load_config(None)
        self.assertEqual(config["telegram_bot_token"], "")
        self.assertEqual(config["allowed_users"], [])
        self.assertEqual(config["claude_timeout"], 120)

    def test_load_from_file(self):
        import tempfile, json
        cfg = {"telegram_bot_token": "abc:123", "allowed_users": [111]}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            with patch.dict(os.environ, {}, clear=True):
                config = agent.load_config(f.name)
        os.unlink(f.name)
        self.assertEqual(config["telegram_bot_token"], "abc:123")
        self.assertEqual(config["allowed_users"], [111])
        self.assertEqual(config["claude_timeout"], 120)  # default preserved

    def test_env_overrides_file(self):
        import tempfile, json
        cfg = {"telegram_bot_token": "from-file", "claude_timeout": 60}
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump(cfg, f)
            f.flush()
            with patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "from-env"}):
                config = agent.load_config(f.name)
        os.unlink(f.name)
        self.assertEqual(config["telegram_bot_token"], "from-env")
        self.assertEqual(config["claude_timeout"], 60)

    def test_missing_file_uses_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = agent.load_config("/nonexistent/config.json")
        self.assertEqual(config["claude_timeout"], 120)

    def test_init_applies_config(self):
        config = {"telegram_bot_token": "tok", "allowed_users": [1, 2], "claude_timeout": 30}
        agent.init(config)
        self.assertEqual(agent.BOT_TOKEN, "tok")
        self.assertEqual(agent.ALLOWED_USERS, {1, 2})
        self.assertEqual(agent.TIMEOUT, 30)
        # Reset
        agent.init(agent.DEFAULT_CONFIG)


if __name__ == "__main__":
    unittest.main()


class TestRealClaude(unittest.TestCase):
    """Integration tests that call the real Claude CLI.

    These require `claude` to be installed and authenticated.
    Skip with: python -m unittest test_agent.TestRealClaude --skip
    """

    def test_simple_question(self):
        """Claude can answer a simple math question."""
        result, _ = agent.call_claude("What is 2+2? Reply with just the number.")
        self.assertIn("4", result)

    def test_respects_cwd(self):
        """Claude responds without error when given a valid cwd."""
        result, _ = agent.call_claude(
            "What directory are you working in? Reply briefly.",
            cwd=os.path.dirname(__file__) or ".",
        )
        # Should get a response, not a crash
        self.assertIsInstance(result, str)
        self.assertNotIn("timed out", result)
        self.assertNotIn("not found", result)

    def test_empty_prompt(self):
        """Empty prompt doesn't crash."""
        result, _ = agent.call_claude("")
        self.assertIsInstance(result, str)

    def test_code_generation(self):
        """Claude can generate code."""
        result, _ = agent.call_claude(
            "Write a Python function that adds two numbers. Only output the code, no explanation."
        )
        self.assertIn("def", result)
        self.assertIn("return", result)

    def test_multi_language(self):
        """Claude can respond in Chinese."""
        result, _ = agent.call_claude("用中文回答：1+1等于几？只回答数字。")
        self.assertIn("2", result)

    def test_file_awareness_with_cwd(self):
        """Claude can read files in the cwd when using --dangerously-skip-permissions."""
        result, _ = agent.call_claude(
            "Read the file README.md in the current directory and tell me the project name. Reply with just the name.",
            cwd=os.path.dirname(__file__) or ".",
        )
        # Should mention our project
        self.assertTrue(
            "telegram" in result.lower() or "claude" in result.lower() or "agent" in result.lower(),
            f"Expected project name in: {result}"
        )

    def test_long_response(self):
        """Claude can produce a longer response."""
        result, _ = agent.call_claude("Count from 1 to 20, one number per line.")
        self.assertIn("1", result)
        self.assertIn("20", result)
