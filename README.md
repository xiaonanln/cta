# CTA — Claude Telegram Agent

Self-hosted Telegram bot powered by Claude Code CLI. Uses your Max/Pro subscription — **no API tokens, no extra cost**.

## Setup

1. Create a Telegram bot via [@BotFather](https://t.me/BotFather)
2. Install Claude Code CLI: `npm install -g @anthropic-ai/claude-code`
3. Install Python deps: `pip install -r requirements.txt`
4. Run:

```bash
TELEGRAM_BOT_TOKEN=your-bot-token \
ALLOWED_USERS=your-telegram-user-id \
python agent.py
```

## Environment Variables

| Variable | Required | Description |
|----------|----------|-------------|
| `TELEGRAM_BOT_TOKEN` | Yes | From @BotFather |
| `ALLOWED_USERS` | No | Comma-separated Telegram user IDs. Empty = allow all |
| `CLAUDE_TIMEOUT` | No | Max seconds per Claude call (default: 120) |

## Commands

- `/start` — Hello
- `/clear` — Clear conversation history

## How it works

Calls `claude --print -p "prompt"` which uses your local Claude Code subscription (Max/Pro). No API key needed, no extra usage charges.

## Limitations

- Subject to Max subscription rate limits
- No tool use (file ops, web search etc.) — just conversation
- Conversation history is in-memory only (lost on restart)
