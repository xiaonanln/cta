# CTA — Claude Telegram Agent

Self-hosted Telegram bot powered by Claude Code CLI. Uses your Max/Pro subscription — **no API tokens, no extra cost**.

## Quick Start

### Option 1: Docker (recommended)

```bash
git clone https://github.com/xiaonanln/cta.git && cd cta

# Configure
mkdir -p data
cp config.example.json data/config.json
# Edit data/config.json — set your bot token and user ID

# Run
docker compose up -d
```

### Option 2: Run directly

```bash
# Prerequisites
npm install -g @anthropic-ai/claude-code   # Claude CLI
pip install -r requirements.txt             # Python deps

# Configure (pick one)
# A) Config file:
cp config.example.json config.json && vim config.json

# B) Environment variables:
export TELEGRAM_BOT_TOKEN=your-bot-token
export ALLOWED_USERS=your-telegram-user-id

# Run
python agent.py
```

## Configuration

Config file (`config.json`) or environment variables:

| Config Key | Env Var | Default | Description |
|---|---|---|---|
| `telegram_bot_token` | `TELEGRAM_BOT_TOKEN` | — | Bot token from [@BotFather](https://t.me/BotFather) |
| `allowed_users` | `ALLOWED_USERS` | `[]` (all) | Comma-separated Telegram user IDs |
| `claude_timeout` | `CLAUDE_TIMEOUT` | `600` | Max seconds per Claude call |
| `model` | `CLAUDE_MODEL` | `claude-opus-4-6` | Claude model to use |
| `sessions_file` | `SESSIONS_FILE` | `sessions.json` | Path to session persistence file |

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Hello message |
| `/clear` | Clear conversation (reset session) |
| `/cd <path>` | Change working directory |
| `/pwd` | Show current working directory |
| `/model <name>` | Switch Claude model (clears session) |
| `/status` | Show model, cwd, and session info |

## How It Works

```
You → Telegram → CTA → claude --print --resume <session> → Claude Code CLI → response → Telegram
```

- Calls `claude --print` with your local Claude Code subscription
- Full tool access: Claude can read/write files, run commands in the working directory
- Session persistence: conversations survive bot restarts via `sessions.json`
- Per-user queues: messages are processed sequentially per user, concurrently across users

## Docker Details

The Docker setup mounts two volumes:

- `./data/` → config and session persistence
- `~/.claude/` → Claude CLI authentication (read-only)

Make sure you've authenticated Claude CLI on the host first (`claude` login).
