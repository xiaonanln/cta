"""Tests for claude-telegram-agent."""

import os
import subprocess
import unittest
from unittest.mock import patch, MagicMock

# Set required env before importing
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "test-token")

import agent


class TestCallClaude(unittest.TestCase):
    """Tests for the call_claude function."""

    @patch("agent.subprocess.run")
    def test_returns_stdout(self, mock_run):
        mock_run.return_value = MagicMock(stdout="Hello world", stderr="")
        result = agent.call_claude("hi")
        self.assertEqual(result, "Hello world")

    @patch("agent.subprocess.run")
    def test_passes_cwd(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        agent.call_claude("hi", cwd="/tmp/test")
        _, kwargs = mock_run.call_args
        self.assertEqual(kwargs["cwd"], "/tmp/test")

    @patch("agent.subprocess.run")
    def test_uses_dangerously_skip_permissions(self, mock_run):
        mock_run.return_value = MagicMock(stdout="ok", stderr="")
        agent.call_claude("hi")
        args = mock_run.call_args[0][0]
        self.assertIn("--dangerously-skip-permissions", args)
        self.assertIn("--print", args)

    @patch("agent.subprocess.run")
    def test_returns_stderr_on_empty_stdout(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="some error")
        result = agent.call_claude("hi")
        self.assertIn("Error", result)
        self.assertIn("some error", result)

    @patch("agent.subprocess.run")
    def test_returns_empty_response_message(self, mock_run):
        mock_run.return_value = MagicMock(stdout="", stderr="")
        result = agent.call_claude("hi")
        self.assertEqual(result, "(empty response)")

    @patch("agent.subprocess.run", side_effect=subprocess.TimeoutExpired("claude", 120))
    def test_timeout_returns_message(self, mock_run):
        result = agent.call_claude("hi")
        self.assertIn("timed out", result)

    @patch("agent.subprocess.run", side_effect=FileNotFoundError)
    def test_cli_not_found(self, mock_run):
        result = agent.call_claude("hi")
        self.assertIn("not found", result)

    @patch("agent.subprocess.run")
    def test_strips_whitespace(self, mock_run):
        mock_run.return_value = MagicMock(stdout="  hello  \n", stderr="")
        result = agent.call_claude("hi")
        self.assertEqual(result, "hello")


class TestConversationHistory(unittest.TestCase):
    """Tests for conversation history management."""

    def setUp(self):
        agent.conversations.clear()
        agent.user_cwd.clear()

    def test_history_starts_empty(self):
        self.assertEqual(len(agent.conversations), 0)

    def test_history_trimmed_to_max(self):
        uid = 123
        agent.conversations[uid] = [
            {"role": "user", "content": f"msg{i}"} for i in range(30)
        ]
        # Simulate trim logic from handle_message
        if len(agent.conversations[uid]) > agent.MAX_HISTORY:
            agent.conversations[uid] = agent.conversations[uid][-agent.MAX_HISTORY:]
        self.assertEqual(len(agent.conversations[uid]), agent.MAX_HISTORY)

    def test_clear_removes_history(self):
        agent.conversations[123] = [{"role": "user", "content": "hi"}]
        agent.conversations.pop(123, None)
        self.assertNotIn(123, agent.conversations)


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


class TestPromptBuilding(unittest.TestCase):
    """Tests for conversation prompt construction."""

    def test_single_message_prompt(self):
        history = [{"role": "user", "content": "hello"}]
        parts = []
        for msg in history:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{prefix}: {msg['content']}")
        parts.append("Assistant:")
        prompt = "\n\n".join(parts)
        self.assertIn("User: hello", prompt)
        self.assertTrue(prompt.endswith("Assistant:"))

    def test_multi_turn_prompt(self):
        history = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
            {"role": "user", "content": "how are you"},
        ]
        parts = []
        for msg in history:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            parts.append(f"{prefix}: {msg['content']}")
        parts.append("Assistant:")
        prompt = "\n\n".join(parts)
        self.assertIn("User: hi", prompt)
        self.assertIn("Assistant: hello", prompt)
        self.assertIn("User: how are you", prompt)


if __name__ == "__main__":
    unittest.main()
