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

# Run all tests (mocked — no real Claude calls)
python -m unittest test_agent test_cron test_notify
```

## Files

- `agent.py` — Main bot code, single file.
- `cron.py` — CLI for managing per-chat cron jobs (calls the local web API).
- `notify.py` — CLI for cross-agent messaging (sends to another chat by label or id).
- `test_agent.py`, `test_cron.py`, `test_notify.py` — Tests (all mocked, no real Claude calls).
- `requirements.txt` — Python dependencies.
- `~/.cta/config.json` — Bot configuration.
- `~/.cta/agents.json` — Per-chat persistence: Claude session_id, cwd, model, last_active, label.
- `~/.cta/memory/<uid>:<chat>.md` — Per-chat agent memory.
- `~/.cta/crons/<uid>:<chat>.json` — Per-chat cron jobs.
- `~/.cta/preamble/<uid>:<chat>.md` — Per-chat custom preamble.
- `~/.cta/global_preamble.md` — Preamble injected into every chat.

## Key Design Decisions

- **`claude --print --dangerously-skip-permissions`** — Full tool access (file read/write, shell commands).
- **Config** — Always `~/.cta/config.json`. No CLI args, no env vars.
- **Per-chat persistence** — `~/.cta/agents.json` holds session_id, cwd, model, last_active, and chat label per `(uid, chat_id)`. Survives restarts.
- **Working directory** — Defaults to `os.getcwd()`. Change with `/cd` command.
- **User whitelist** — `allowed_users` in config.json. Empty = allow all.
- **Concurrency** — Claude calls run in parallel across different chats; per-chat they're serialized via `claude_active_keys`. Anthropic rate limits still apply.
- **Bot initialization** — Lazy (in `create_bot()`) so tests can import without a real token.
- **CTA_UID / CTA_CHAT_ID env vars** — Injected into every Claude subprocess so helper scripts (`cron.py`, `notify.py`) know which chat is calling them.

## Telegram Bot Commands

- `/start` — Hello message
- `/clear` — Clear conversation (reset session)
- `/cd <path>` — Change working directory
- `/pwd` — Show current working directory
- `/model <name>` — Switch Claude model (keeps session)
- `/opus`, `/sonnet` — Shortcuts for switching to Opus / Sonnet
- `/timeout <seconds>` — Override the per-chat Claude timeout
- `/status` — Show model, cwd, and session info
- `/cancel` — Kill an in-flight Claude call for this chat

## Testing

- All tests use `unittest.mock.patch` — zero Claude calls.
- Bot handlers tested with fake bot (`MagicMock`).
- Concurrent user tests use threading barriers.

## Guidelines

- Keep it simple — single file, no frameworks, no database.
- All changes must pass `python -m unittest test_agent -v`.
- Don't hardcode paths or tokens.
- Always create a PR after completing a task. Commit changes, push to a feature branch, and open a PR against main.
