# CLAUDE.md — CTA (Claude Telegram Agent)

Self-hosted Telegram bot that uses Claude Code CLI (`claude --print`) as the backend. Runs on Max/Pro subscription — no API tokens, no extra cost.

## Architecture

```
Telegram → agent.py (pyTelegramBotAPI) → subprocess claude --print → response → Telegram
```

Single Python file. Config and sessions in `~/.cta/`.

## Common Commands

```sh
# Run the bot (reads ~/.cta/config.json)
python agent.py

# Run tests (mock only, no Claude calls)
python -m unittest test_agent.TestConfig test_agent.TestSessionPersistence test_agent.TestCallClaude test_agent.TestSplitReply test_agent.TestSendMarkdown test_agent.TestAllowed test_agent.TestTuiLog test_agent.TestBotHandlers test_agent.TestUserCwd test_agent.TestUserModel test_agent.TestConcurrentUsers -v

# Run real Claude integration tests (requires claude CLI authenticated)
python -m unittest test_agent.TestRealClaude -v

# Run all tests
python -m unittest test_agent -v
```

## Files

- `agent.py` — Main bot code, single file.
- `test_agent.py` — Tests (mock + real Claude).
- `requirements.txt` — Python dependencies.
- `~/.cta/config.json` — Bot configuration.
- `~/.cta/sessions.json` — Session persistence.

## Key Design Decisions

- **`claude --print --dangerously-skip-permissions`** — Full tool access (file read/write, shell commands).
- **Config** — Always `~/.cta/config.json`. No CLI args, no env vars.
- **Sessions** — Persisted in `~/.cta/sessions.json`. Survives restarts.
- **Working directory** — Defaults to `os.getcwd()`. Change with `/cd` command.
- **User whitelist** — `allowed_users` in config.json. Empty = allow all.
- **Concurrency** — Claude CLI calls serialized (Max subscription limit). Waiting users notified.
- **Bot initialization** — Lazy (in `create_bot()`) so tests can import without a real token.

## Telegram Bot Commands

- `/start` — Hello message
- `/clear` — Clear conversation (reset session)
- `/cd <path>` — Change working directory
- `/pwd` — Show current working directory
- `/model <name>` — Switch Claude model (clears session)
- `/status` — Show model, cwd, and session info

## Testing

- Mock tests use `unittest.mock.patch` — zero Claude calls.
- Real tests call actual `claude --print` — require authenticated CLI.
- Bot handlers tested with fake bot (`MagicMock`).
- Concurrent user tests use threading barriers.

## Guidelines

- Keep it simple — single file, no frameworks, no database.
- All changes must pass `python -m unittest test_agent -v`.
- Don't hardcode paths or tokens.
- Always create a PR after completing a task. Commit changes, push to a feature branch, and open a PR against main.
