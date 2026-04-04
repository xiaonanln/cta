#!/usr/bin/env python3
"""
Telegram bot powered by Claude Code CLI.
Uses Max subscription — no API tokens needed.
"""

import os
import subprocess
import threading
import telebot

BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
ALLOWED_USERS = set(
    int(x) for x in os.environ.get("ALLOWED_USERS", "").split(",") if x.strip()
)
TIMEOUT = int(os.environ.get("CLAUDE_TIMEOUT", "120"))
DEFAULT_CWD = os.environ.get("CLAUDE_CWD", os.path.expanduser("~"))

bot = telebot.TeleBot(BOT_TOKEN)

# Per-user state
conversations: dict[int, list[dict]] = {}
user_cwd: dict[int, str] = {}  # per-user working directory
MAX_HISTORY = 20


def call_claude(prompt: str, cwd: str = DEFAULT_CWD) -> str:
    """Call Claude Code CLI in print mode (uses Max subscription)."""
    try:
        result = subprocess.run(
            ["claude", "--print", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=TIMEOUT,
            cwd=cwd,
        )
        output = result.stdout.strip()
        if not output and result.stderr:
            output = f"[Error] {result.stderr.strip()}"
        return output or "(empty response)"
    except subprocess.TimeoutExpired:
        return "(Claude timed out)"
    except FileNotFoundError:
        return "(claude CLI not found — install @anthropic-ai/claude-code)"


@bot.message_handler(commands=["start"])
def cmd_start(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    bot.reply_to(message, "👋 Hi! I'm powered by Claude Code CLI. Just send me a message.")


@bot.message_handler(commands=["clear"])
def cmd_clear(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    conversations.pop(message.from_user.id, None)
    bot.reply_to(message, "🧹 Conversation cleared.")


@bot.message_handler(commands=["cd"])
def cmd_cd(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    path = message.text.replace("/cd", "", 1).strip()
    if not path:
        cwd = user_cwd.get(message.from_user.id, DEFAULT_CWD)
        bot.reply_to(message, f"📂 Current: `{cwd}`", parse_mode="Markdown")
        return
    expanded = os.path.expanduser(path)
    if os.path.isdir(expanded):
        user_cwd[message.from_user.id] = expanded
        bot.reply_to(message, f"📂 → `{expanded}`", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ Not a directory: `{path}`", parse_mode="Markdown")


@bot.message_handler(commands=["pwd"])
def cmd_pwd(message):
    if ALLOWED_USERS and message.from_user.id not in ALLOWED_USERS:
        return
    cwd = user_cwd.get(message.from_user.id, DEFAULT_CWD)
    bot.reply_to(message, f"📂 `{cwd}`", parse_mode="Markdown")


@bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))
def handle_message(message):
    uid = message.from_user.id
    if ALLOWED_USERS and uid not in ALLOWED_USERS:
        return

    # Build conversation context
    if uid not in conversations:
        conversations[uid] = []
    conversations[uid].append({"role": "user", "content": message.text})

    # Trim history
    if len(conversations[uid]) > MAX_HISTORY:
        conversations[uid] = conversations[uid][-MAX_HISTORY:]

    # Build prompt with history
    prompt_parts = []
    for msg in conversations[uid]:
        prefix = "User" if msg["role"] == "user" else "Assistant"
        prompt_parts.append(f"{prefix}: {msg['content']}")
    prompt_parts.append("Assistant:")
    prompt = "\n\n".join(prompt_parts)

    # Call Claude in background thread to not block
    cwd = user_cwd.get(uid, DEFAULT_CWD)

    def process():
        reply = call_claude(prompt, cwd=cwd)
        conversations[uid].append({"role": "assistant", "content": reply})
        # Telegram max message length = 4096
        for i in range(0, len(reply), 4096):
            bot.reply_to(message, reply[i : i + 4096])

    threading.Thread(target=process, daemon=True).start()


if __name__ == "__main__":
    if not BOT_TOKEN:
        print("Error: TELEGRAM_BOT_TOKEN not set")
        print("Usage: TELEGRAM_BOT_TOKEN=xxx ALLOWED_USERS=123,456 python agent.py")
        exit(1)
    print(f"Bot starting... (allowed users: {ALLOWED_USERS or 'all'})")
    bot.infinity_polling()
