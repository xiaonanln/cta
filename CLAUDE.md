# CLAUDE.md — CTA (Claude Telegram Agent)

Self-hosted Telegram bot that uses Claude Code CLI (`claude --print`) as the backend. Runs on Max/Pro subscription — no API tokens, no extra cost.

## Architecture

```
Telegram → bot.py (pyTelegramBotAPI) → subprocess claude --print → response → Telegram
```

Single Python file. No server, no database, no API keys.

## Common Commands

```sh
# Run the bot (configure config.json first)
python agent.py

# Run tests (mock only, no Claude calls)
python -m unittest test_agent.TestConfig test_agent.TestSessionPersistence test_agent.TestCallClaude test_agent.TestSplitReply test_agent.TestSendMarkdown test_agent.TestAllowed test_agent.TestTuiLog test_agent.TestBotHandlers test_agent.TestUserCwd test_agent.TestUserModel -v

# Run real Claude integration tests (requires claude CLI authenticated)
python -m unittest test_agent.TestRealClaude -v

# Run all tests
python -m unittest test_agent -v
```

## Files

- `agent.py` — Main bot code. Single file, ~100 lines.
- `test_agent.py` — 38 tests (31 mock + 7 real Claude).
- `requirements.txt` — Just `pyTelegramBotAPI`.

## Key Design Decisions

- **`claude --print --dangerously-skip-permissions`** — Full tool access (file read/write, shell commands).
- **Working directory** — Defaults to `os.getcwd()`. Change with `/cd` command or `CLAUDE_CWD` env.
- **Conversation history** — In-memory, per-user, max 20 messages. Lost on restart.
- **User whitelist** — `ALLOWED_USERS` env var (comma-separated Telegram user IDs). Empty = allow all.
- **Bot initialization** — Lazy (in `create_bot()`) so tests can import without a real token.

## Telegram Bot Commands

- `/start` — Hello message
- `/clear` — Clear conversation history
- `/cd <path>` — Change working directory
- `/pwd` — Show current working directory

## Testing

- Mock tests use `unittest.mock.patch` — zero Claude calls.
- Real tests call actual `claude --print` — require authenticated CLI.
- Bot handlers tested with fake bot (`MagicMock`).

## Guidelines

- Keep it simple — single file, no frameworks, no database.
- All changes must pass `python -m unittest test_agent -v`.
- Don't hardcode paths or tokens.
