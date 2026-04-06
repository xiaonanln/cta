# CTA — Claude Telegram Agent

Self-hosted Telegram bot powered by Claude Code CLI. Uses your Max/Pro subscription — **no API tokens, no extra cost**.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
2. Install Claude Code CLI: `npm install -g @anthropic-ai/claude-code`
3. Install Python deps: `pip install -r requirements.txt`
4. Configure:

```bash
python agent.py  # auto-generates ~/.cta/config.json on first run
# Edit ~/.cta/config.json — set telegram_bot_token and allowed_users
```

5. Run:

```bash
python agent.py
```

## Configuration

All configuration is in `~/.cta/config.json`:

| Key | Default | Description |
|---|---|---|
| `telegram_bot_token` | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `allowed_users` | `[]` (all) | List of Telegram user IDs |
| `claude_timeout` | `600` | Max seconds per Claude call |
| `model` | `claude-sonnet-4-6` | Claude model to use |

Sessions and working directories are persisted in `~/.cta/sessions.json` automatically — both are restored on restart.

Each chat has a memory file at `~/.cta/memory/<uid>:<chat_id>.md`. Claude reads it at the start of every message for context and appends new facts worth remembering — no setup required.

## Bot Commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/start` | Hello message |
| `/clear` | Clear conversation (reset session) |
| `/cd <path>` | Change Claude's working directory (creates it if needed) |
| `/pwd` | Show current working directory |
| `/model <name>` | Switch Claude model (clears session) |
| `/status` | Show model, cwd, and session info |

## How It Works

```
You → Telegram → CTA → claude --print --resume <session> → response → Telegram
```

- Calls `claude --print --dangerously-skip-permissions` with your local Claude Code subscription
- Full tool access: Claude can read/write files and run commands in the working directory
- **Separate sessions per context**: DMs and each group chat maintain independent Claude conversations — switching between them never bleeds context
- Session persistence: conversations survive restarts via `~/.cta/sessions.json`
- Per-user message queues: sequential processing per user, serialized Claude calls
- Automatic retry on transient Claude failures (up to 2 retries)
- File/image support: send any document or image and Claude will read and analyze it
- Per-chat memory: Claude maintains a memory file per chat, persisting context across sessions automatically
- Rich TUI status panel: each active chat shown as a card with label, model, cwd, and message count; active session highlighted in yellow
- Markdown formatting via `telegramify-markdown`
