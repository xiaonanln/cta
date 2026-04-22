# CTA — Claude Telegram Agent

**Use your Claude Max subscription from Telegram — no API tokens, no extra cost.**

CTA is a self-hosted Telegram bot that connects to Claude Code CLI running on your machine. If you already pay for Claude Max or Pro, you get a full-featured AI assistant on Telegram for free.

## Why CTA

- **Zero extra cost** — runs on your existing Max/Pro subscription
- **Full tool access** — Claude can read/write files and run shell commands on your machine
- **Persistent sessions** — conversations survive restarts; each chat has independent context
- **Per-chat memory** — Claude automatically remembers facts across sessions
- **Web dashboard** — monitor and chat from a browser, no terminal needed
- **Voice messages** — transcribed via Whisper and sent to Claude
- **Multi-chat** — DMs and group chats each maintain separate Claude conversations

## Prerequisites

- Claude Max or Pro subscription
- Claude Code CLI installed and authenticated (`claude` works in your terminal)
- Python 3.9+
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

**1. Install Claude Code CLI**
```bash
npm install -g @anthropic-ai/claude-code
claude  # authenticate with your Anthropic account
```

**2. Clone and install dependencies**
```bash
git clone https://github.com/xiaonanln/cta.git
cd cta
pip install -r requirements.txt
```

**3. Configure**
```bash
python agent.py  # generates ~/.cta/config.json on first run
```

Edit `~/.cta/config.json`:
```json
{
  "telegram_bot_token": "your-token-from-botfather",
  "allowed_users": [123456789],
  "model": "claude-sonnet-4-6"
}
```

Leave `allowed_users` empty to allow anyone who messages the bot.

**4. Run**
```bash
python agent.py
```

Open the web dashboard at `http://localhost:17488/`.

## Configuration

| Key | Default | Description |
|---|---|---|
| `telegram_bot_token` | — | From [@BotFather](https://t.me/BotFather) |
| `allowed_users` | `[]` (all) | Telegram user IDs to whitelist |
| `claude_timeout` | `600` | Max seconds per Claude call |
| `model` | `claude-sonnet-4-6` | Claude model |
| `web_port` | `17488` | Web dashboard port |

Sessions and working directories persist in `~/.cta/sessions.json` and survive restarts. Each chat has a memory file at `~/.cta/memory/<uid>:<chat_id>.md` — Claude reads and updates it automatically.

## Bot Commands

| Command | Description |
|---|---|
| `/help` | List all commands |
| `/clear` | Reset conversation |
| `/cd <path>` | Change Claude's working directory |
| `/pwd` | Show current working directory |
| `/model <name>` | Switch Claude model |
| `/status` | Show model, cwd, and session info |

## How It Works

```
Telegram → agent.py → claude --print --resume <session> → Telegram
```

CTA calls `claude --print --dangerously-skip-permissions` as a subprocess, resuming conversations via `--resume`. Claude has full tool access in the configured working directory — it can read/write files, run commands, and use all Claude Code tools.

## Web Dashboard

Open `http://localhost:<web_port>/` while the bot is running:

- **Chats** — a card per active chat with model, working directory, and last reply preview
- **Log** — live-streaming event log via SSE
- **Status** — current model, default cwd, and session summary

## Run as a Service (macOS)

Create `~/Library/LaunchAgents/com.cta.agent.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.cta.agent</string>
    <key>ProgramArguments</key>
    <array>
        <string>/path/to/venv/bin/python3</string>
        <string>/path/to/cta/agent.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>/path/to/cta</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>/Users/you/.cta/cta.log</string>
    <key>StandardErrorPath</key>
    <string>/Users/you/.cta/cta.err</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.cta.agent.plist
```

## License

MIT
