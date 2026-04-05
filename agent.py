#!/usr/bin/env python3
"""
CTA — Claude Telegram Agent.
Telegram bot powered by Claude Code CLI.
Uses Max subscription — no API tokens needed.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from datetime import datetime

import telebot
import telegramify_markdown
from rich.layout import Layout
from rich.live import Live
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

# ── Config ────────────────────────────────────────────────────────────────────

CTA_HOME = os.path.expanduser("~/.cta")
CONFIG_PATH = os.path.join(CTA_HOME, "config.json")

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_users": [],
    "claude_timeout": 600,
    "model": "claude-sonnet-4-6",
}


def load_config() -> dict:
    """Load config from ~/.cta/config.json. Creates a template if not found."""
    config = dict(DEFAULT_CONFIG)

    if not os.path.exists(CONFIG_PATH):
        try:
            os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
            with open(CONFIG_PATH, "w") as f:
                json.dump(DEFAULT_CONFIG, f, indent=2)
                f.write("\n")
            print(f"Created config template at {CONFIG_PATH} — fill in telegram_bot_token to get started.")
        except OSError:
            pass
    else:
        with open(CONFIG_PATH) as f:
            config.update(json.load(f))

    return config


# ── Globals ───────────────────────────────────────────────────────────────────

BOT_TOKEN = ""
ALLOWED_USERS: set[int] = set()
TIMEOUT = 600
MODEL = "claude-sonnet-4-6"
DEFAULT_CWD = os.getcwd()
SESSIONS_PATH = os.path.join(CTA_HOME, "sessions.json")

bot = None  # initialized in create_bot()
user_sessions: dict[tuple[int, int], str] = {}  # (uid, chat_id) → Claude session ID
user_cwd: dict[tuple[int, int], str] = {}  # (uid, chat_id) → working directory
user_model: dict[tuple[int, int], str] = {}  # (uid, chat_id) → model override
user_queues: dict[tuple[int, int], queue.Queue] = {}
user_queues_lock = threading.Lock()
chat_labels: dict[tuple[int, int], str] = {}   # (uid, chat_id) → "DM" or group name
msg_counts: dict[tuple[int, int], int] = {}    # (uid, chat_id) → messages processed
claude_lock = threading.Lock()  # serialize Claude CLI calls (Max subscription concurrency limit)
claude_busy_for = None  # username of user currently calling Claude


def init(config: dict):
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, MODEL
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    MODEL = config.get("model", "claude-sonnet-4-6")


def load_sessions():
    if not os.path.exists(SESSIONS_PATH):
        return
    try:
        with open(SESSIONS_PATH) as f:
            data = json.load(f)
        for key_str, session_id in data.items():
            uid_str, chat_str = key_str.split(":", 1)
            user_sessions[(int(uid_str), int(chat_str))] = session_id
        tui_log(f"[dim]Loaded {len(data)} session(s) from {SESSIONS_PATH}[/]")
    except Exception as e:
        tui_log(f"[red]Warning: could not load sessions: {escape(str(e))}[/]")


def save_sessions():
    tmp = SESSIONS_PATH + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump({f"{uid}:{chat_id}": sid for (uid, chat_id), sid in user_sessions.items()}, f)
        os.replace(tmp, SESSIONS_PATH)
    except Exception as e:
        tui_log(f"[red]Warning: could not save sessions: {escape(str(e))}[/]")


# ── TUI ───────────────────────────────────────────────────────────────────────

_log_entries: deque[tuple[str, str]] = deque(maxlen=200)
_tui_lock = threading.Lock()


def tui_log(text: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _tui_lock:
        _log_entries.append((ts, text))


class _TuiLogHandler(logging.Handler):
    def emit(self, record):
        tui_log(f"[dim]{escape(self.format(record))}[/]")


def _status_panel() -> tuple[Panel, int]:
    line1 = f"[bold]default model:[/] [cyan]{escape(MODEL)}[/]   [bold]default cwd:[/] [cyan]{escape(DEFAULT_CWD)}[/]"
    all_keys = set(user_sessions) | set(msg_counts)
    if all_keys:
        rows = []
        for key in sorted(all_keys):
            uid, chat_id = key
            label = escape(chat_labels.get(key, f"{uid}:{chat_id}"))
            sid = user_sessions.get(key, "")
            sid_str = f"[dim]{sid[:8]}…[/]" if sid else "[dim]no session[/]"
            model = escape(user_model.get(key, MODEL))
            cwd = escape(user_cwd.get(key, DEFAULT_CWD))
            count = msg_counts.get(key, 0)
            rows.append(f"  [bold]{label}[/] {sid_str}  model=[cyan]{model}[/]  cwd=[cyan]{cwd}[/]  msgs=[yellow]{count}[/]")
        info = line1 + "\n" + "\n".join(rows)
        size = 3 + len(rows)
    else:
        info = line1 + "   [bold]sessions:[/] [dim]none[/]"
        size = 3
    return Panel(info, title="[bold green]CTA[/]"), size


def _log_panel() -> Panel:
    table = Table(box=None, show_header=False, padding=(0, 1), expand=True)
    table.add_column("time", style="dim", width=8, no_wrap=True)
    table.add_column("text")
    with _tui_lock:
        entries = list(_log_entries)
    for ts, text in entries:
        table.add_row(ts, text)
    return Panel(table, title="[bold]Log[/]")


def _build_layout() -> Layout:
    panel, size = _status_panel()
    layout = Layout()
    layout.split_column(Layout(name="status", size=size), Layout(name="log"))
    layout["status"].update(panel)
    layout["log"].update(_log_panel())
    return layout


# ── Claude CLI ────────────────────────────────────────────────────────────────

def call_claude(prompt: str, cwd: str = None, session_id: str = None, model: str = None) -> tuple[str, str]:
    """Call Claude Code CLI. Returns (text, session_id).

    Serialized with claude_lock because Max/Pro subscriptions only allow
    one concurrent CLI session — a second call would hang or error.
    """
    cwd = cwd or DEFAULT_CWD
    cmd = ["claude", "--print", "--dangerously-skip-permissions",
           "--model", model or MODEL, "--output-format", "json", "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, cwd=cwd)
        if not result.stdout.strip():
            err = result.stderr.strip()
            return (f"[Error] {err}" if err else "(empty response)"), ""
        data = json.loads(result.stdout)
        text = (data.get("result") or "").strip() or "(empty response)"
        return text, data.get("session_id", "")
    except subprocess.TimeoutExpired:
        return "(Claude timed out)", ""
    except FileNotFoundError:
        return "(claude CLI not found — install @anthropic-ai/claude-code)", ""


# ── Message processing ────────────────────────────────────────────────────────

def _send_markdown(message, text: str):
    """Send text with MarkdownV2 formatting, falling back to plain text."""
    try:
        bot.reply_to(message, telegramify_markdown.markdownify(text), parse_mode="MarkdownV2")
    except Exception:
        bot.reply_to(message, text)


def _split_reply(text: str, limit: int = 4096) -> list[str]:
    """Split reply into Telegram-sized chunks, preferring newline boundaries."""
    chunks = []
    while len(text) > limit:
        split_at = text.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks


def _typing_loop(chat_id: int, done: threading.Event):
    """Send typing action immediately, then every 4s until done."""
    while True:
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        if done.wait(timeout=4):
            break


def _process_message(uid: int, chat_id: int, message, done: threading.Event):
    global claude_busy_for
    key = (uid, chat_id)
    cwd = user_cwd.get(key, DEFAULT_CWD)
    model = user_model.get(key, MODEL)
    session_id = user_sessions.get(key)
    username = message.from_user.username or str(uid)
    if message.chat.type == "private":
        chat_labels[key] = f"DM:{username}"
    else:
        chat_labels[key] = message.chat.title or str(chat_id)

    # If Claude is busy with another user, notify and wait
    if claude_lock.locked() and claude_busy_for != username:
        bot.reply_to(message, f"⏳ Waiting for @{claude_busy_for} to finish...")
        tui_log(f"[yellow]⏳[/] [bold]{escape(username)}[/] queued (busy: {escape(claude_busy_for or '?')})")

    with claude_lock:
        claude_busy_for = username
        try:
            reply, new_session_id = call_claude(message.text, cwd=cwd, session_id=session_id, model=model)
            if session_id and "No conversation found with session ID" in reply:
                tui_log(f"[yellow]⚠ stale session for {escape(username)}, retrying fresh[/]")
                user_sessions.pop(key, None)
                reply, new_session_id = call_claude(message.text, cwd=cwd, session_id=None, model=model)
        finally:
            claude_busy_for = None
            done.set()

    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[blue]←[/] [bold]{escape(username)}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    for chunk in _split_reply(reply):
        _send_markdown(message, chunk)


def _user_worker(uid: int, chat_id: int, q: queue.Queue):
    while True:
        message = q.get()
        done = threading.Event()
        threading.Thread(target=_typing_loop, args=(chat_id, done), daemon=True).start()
        try:
            _process_message(uid, chat_id, message, done)
        except Exception as e:
            done.set()
            tui_log(f"[red][worker:{uid}:{chat_id}] error: {escape(str(e))}[/]")
        finally:
            q.task_done()


def _get_user_queue(uid: int, chat_id: int) -> queue.Queue:
    key = (uid, chat_id)
    with user_queues_lock:
        if key not in user_queues:
            q = queue.Queue()
            user_queues[key] = q
            threading.Thread(target=_user_worker, args=(uid, chat_id, q), daemon=True).start()
        return user_queues[key]


# ── Bot handlers ──────────────────────────────────────────────────────────────

def _allowed(message) -> bool:
    return not ALLOWED_USERS or message.from_user.id in ALLOWED_USERS


def cmd_start(message):
    if not _allowed(message): return
    bot.reply_to(message, "👋 Hi! I'm powered by Claude Code CLI. Just send me a message.")


def cmd_clear(message):
    if not _allowed(message): return
    user_sessions.pop((message.from_user.id, message.chat.id), None)
    save_sessions()
    bot.reply_to(message, "🧹 Conversation cleared.")


def cmd_cd(message):
    if not _allowed(message): return
    uid = message.from_user.id
    path = message.text.replace("/cd", "", 1).strip()
    if not path:
        bot.reply_to(message, f"📂 Current: `{user_cwd.get((uid, message.chat.id), DEFAULT_CWD)}`", parse_mode="Markdown")
        return
    expanded = os.path.expanduser(path)
    if os.path.isdir(expanded):
        user_cwd[(uid, message.chat.id)] = expanded
        user_sessions.pop((uid, message.chat.id), None)
        save_sessions()
        bot.reply_to(message, f"📂 → `{expanded}` (session cleared)", parse_mode="Markdown")
    else:
        bot.reply_to(message, f"❌ Not a directory: `{path}`", parse_mode="Markdown")


def cmd_pwd(message):
    if not _allowed(message): return
    bot.reply_to(message, f"📂 `{user_cwd.get((message.from_user.id, message.chat.id), DEFAULT_CWD)}`", parse_mode="Markdown")


def cmd_model(message):
    if not _allowed(message): return
    uid = message.from_user.id
    name = message.text.replace("/model", "", 1).strip()
    if not name:
        bot.reply_to(message, f"🤖 Model: `{user_model.get((uid, message.chat.id), MODEL)}`", parse_mode="Markdown")
        return
    user_model[(uid, message.chat.id)] = name
    user_sessions.pop((uid, message.chat.id), None)
    save_sessions()
    bot.reply_to(message, f"🤖 Model → `{name}` (session cleared)", parse_mode="Markdown")


def cmd_status(message):
    if not _allowed(message): return
    uid = message.from_user.id
    bot.reply_to(
        message,
        f"🤖 Model: `{user_model.get((uid, message.chat.id), MODEL)}`\n"
        f"📂 Cwd: `{user_cwd.get((uid, message.chat.id), DEFAULT_CWD)}`\n"
        f"🔑 Session: `{user_sessions.get((uid, message.chat.id), 'none')}`",
        parse_mode="Markdown",
    )


def handle_message(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        source = "[DM]"
    else:
        source = f"[Group: {escape(message.chat.title or str(message.chat.id))}]"
    tui_log(f"[green]→[/] {source} [bold]{escape(str(message.from_user.username or uid))}[/] {escape(message.text)}")
    if not _allowed(message):
        return
    _get_user_queue(uid, message.chat.id).put(message)


# ── Bot setup ─────────────────────────────────────────────────────────────────

class _Suppress409(logging.Filter):
    def filter(self, record):
        return "409" not in record.getMessage()


def create_bot():
    global bot
    telebot_log = logging.getLogger("TeleBot")
    telebot_log.addFilter(_Suppress409())
    telebot_log.addHandler(_TuiLogHandler())
    telebot_log.propagate = False
    bot = telebot.TeleBot(BOT_TOKEN, num_threads=8)
    bot.message_handler(commands=["start"])(cmd_start)
    bot.message_handler(commands=["clear"])(cmd_clear)
    bot.message_handler(commands=["cd"])(cmd_cd)
    bot.message_handler(commands=["pwd"])(cmd_pwd)
    bot.message_handler(commands=["model"])(cmd_model)
    bot.message_handler(commands=["status"])(cmd_status)
    bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))(handle_message)
    return bot


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = load_config()
    init(config)

    if not BOT_TOKEN:
        print(f"Error: telegram_bot_token not set in {CONFIG_PATH}")
        exit(1)

    load_sessions()
    create_bot()
    tui_log(f"[dim]CTA starting… model=[cyan]{MODEL}[/] cwd=[cyan]{DEFAULT_CWD}[/] users={ALLOWED_USERS or 'all'}[/]")

    for uid in ALLOWED_USERS or set(user_sessions.keys()):
        try:
            bot.send_message(uid, "✅ CTA is ready.")
        except Exception as e:
            tui_log(f"[red]Could not notify {uid}: {escape(str(e))}[/]")

    threading.Thread(target=bot.infinity_polling, daemon=True).start()
    with Live(auto_refresh=False, screen=True) as live:
        while True:
            live.update(_build_layout())
            live.refresh()
            time.sleep(0.25)
