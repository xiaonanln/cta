# CTA — Claude Telegram Agent

Self-hosted Telegram bot powered by Claude Code CLI. Uses your Max/Pro subscription — **no API tokens, no extra cost**.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
2. Install Claude Code CLI: `npm install -g @anthropic-ai/claude-code`
3. Install Python deps: `pip install -r requirements.txt`
4. Configure:

```bash
cp config.example.json config.json
# Edit config.json — set telegram_bot_token and allowed_users
```

5. Run:

```bash
python agent.py
```

## Configuration

All configuration is in `config.json` (default path, override with `-f`):

| Key | Default | Description |
|---|---|---|
| `telegram_bot_token` | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `allowed_users` | `[]` (all) | List of Telegram user IDs |
| `claude_timeout` | `600` | Max seconds per Claude call |
| `model` | `claude-opus-4-6` | Claude model to use |
| `sessions_file` | `sessions.json` | Path to session persistence file |

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Hello message |
| `/clear` | Clear conversation (reset session) |
| `/cd <path>` | Change Claude's working directory |
| `/pwd` | Show current working directory |
| `/model <name>` | Switch Claude model (clears session) |
| `/status` | Show model, cwd, and session info |

## How It Works

```
You → Telegram → CTA → claude --print --resume <session> → response → Telegram
```

- Calls `claude --print --dangerously-skip-permissions` with your local Claude Code subscription
- Full tool access: Claude can read/write files and run commands in the working directory
- Session persistence: conversations survive restarts via `sessions.json`
- Per-user message queues: sequential processing per user, concurrent across users
- Markdown formatting via `telegramify-markdown`
