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
import re
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import telebot
import telegramify_markdown
from flask import Flask, Response, stream_with_context
from rich.markup import escape

# ── Config ────────────────────────────────────────────────────────────────────

CTA_HOME = os.path.expanduser("~/.cta")
CONFIG_PATH = os.path.join(CTA_HOME, "config.json")

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_users": [],
    "claude_timeout": 600,
    "model": "claude-sonnet-4-6",
    "web_port": 17488,
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
WEB_PORT = 17488
DEFAULT_CWD = os.getcwd()
SESSIONS_PATH = os.path.join(CTA_HOME, "sessions.json")
MEMORY_DIR = os.path.join(CTA_HOME, "memory")
CRONS_DIR = os.path.join(CTA_HOME, "crons")

bot = None  # initialized in create_bot()
user_sessions: dict[tuple[int, int], str] = {}  # (uid, chat_id) → Claude session ID
user_cwd: dict[tuple[int, int], str] = {}  # (uid, chat_id) → working directory
user_model: dict[tuple[int, int], str] = {}  # (uid, chat_id) → model override
user_timeout: dict[tuple[int, int], int] = {}  # (uid, chat_id) → timeout override (seconds)
user_queues: dict[tuple[int, int], queue.Queue] = {}
user_queues_lock = threading.Lock()
chat_labels: dict[tuple[int, int], str] = {}   # (uid, chat_id) → "DM" or group name
msg_counts: dict[tuple[int, int], int] = {}    # (uid, chat_id) → messages processed
claude_lock = threading.Lock()  # serialize Claude CLI calls (Max subscription concurrency limit)
claude_busy_for = None  # username of user currently calling Claude
claude_busy_key = None  # (uid, chat_id) of active session


def init(config: dict):
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, MODEL, WEB_PORT
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    MODEL = config.get("model", "claude-sonnet-4-6")
    WEB_PORT = config.get("web_port", 17488)
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(CRONS_DIR, exist_ok=True)


def load_sessions():
    if not os.path.exists(SESSIONS_PATH):
        return
    try:
        with open(SESSIONS_PATH) as f:
            data = json.load(f)
        for key_str, entry in data.items():
            uid_str, chat_str = key_str.split(":", 1)
            key = (int(uid_str), int(chat_str))
            if isinstance(entry, str):  # backward compat
                user_sessions[key] = entry
            else:
                if entry.get("session"):
                    user_sessions[key] = entry["session"]
                if entry.get("cwd"):
                    user_cwd[key] = entry["cwd"]
        tui_log(f"[dim]Loaded {len(data)} session(s) from {SESSIONS_PATH}[/]")
    except Exception as e:
        tui_log(f"[red]Warning: could not load sessions: {escape(str(e))}[/]")


def save_sessions():
    tmp = SESSIONS_PATH + ".tmp"
    try:
        all_keys = set(user_sessions) | set(user_cwd)
        data = {}
        for key in all_keys:
            uid, chat_id = key
            entry = {}
            if key in user_sessions:
                entry["session"] = user_sessions[key]
            if key in user_cwd:
                entry["cwd"] = user_cwd[key]
            data[f"{uid}:{chat_id}"] = entry
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SESSIONS_PATH)
    except Exception as e:
        tui_log(f"[red]Warning: could not save sessions: {escape(str(e))}[/]")


# ── Cron scheduler ────────────────────────────────────────────────────────────

def _cron_path(uid: int, chat_id: int) -> str:
    return os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json")


def _load_cron_jobs(uid: int, chat_id: int) -> list:
    path = _cron_path(uid, chat_id)
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return []


def _save_cron_jobs(uid: int, chat_id: int, jobs: list):
    path = _cron_path(uid, chat_id)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        tui_log(f"[red]Warning: could not save crons for {uid}:{chat_id}: {escape(str(e))}[/]")


def _cron_scheduler():
    """Background thread: fires due cron jobs into user queues every 60s."""
    from croniter import croniter
    while True:
        time.sleep(60)
        now = datetime.now()
        if not os.path.isdir(CRONS_DIR):
            continue
        for fname in os.listdir(CRONS_DIR):
            if not fname.endswith(".json"):
                continue
            try:
                uid_str, chat_str = fname[:-5].split(":", 1)
                uid, chat_id = int(uid_str), int(chat_str)
            except ValueError:
                continue
            jobs = _load_cron_jobs(uid, chat_id)
            changed = False
            for job in jobs:
                try:
                    next_run = datetime.fromisoformat(job["next_run"])
                    if next_run <= now:
                        _get_user_queue(uid, chat_id).put({
                            "_type": "cron",
                            "uid": uid,
                            "chat_id": chat_id,
                            "job_id": job["id"],
                            "prompt": job["prompt"],
                        })
                        tui_log(f"[magenta]⏰ cron[/] {uid}:{chat_id} job={job['id']}")
                        cron = croniter(job["schedule"], now)
                        job["next_run"] = cron.get_next(datetime).isoformat()
                        changed = True
                except Exception as e:
                    tui_log(f"[red]cron error {uid}:{chat_id} job={job.get('id')}: {escape(str(e))}[/]")
            if changed:
                _save_cron_jobs(uid, chat_id, jobs)


# ── Web interface ─────────────────────────────────────────────────────────────

_log_entries: deque[tuple[str, str]] = deque(maxlen=200)
_tui_lock = threading.Lock()
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()
_RICH_TAG = re.compile(r"\[/?[^\]]*\]")

app = Flask(__name__)
app.logger.disabled = True
logging.getLogger("werkzeug").setLevel(logging.ERROR)


def _strip_rich(text: str) -> str:
    """Strip Rich markup tags for plain-text display."""
    return _RICH_TAG.sub("", text).replace("\\[", "[")


def tui_log(text: str) -> None:
    ts = datetime.now().strftime("%H:%M:%S")
    with _tui_lock:
        _log_entries.append((ts, text))
    event = {"ts": ts, "text": _strip_rich(text)}
    with _sse_lock:
        dead = [q for q in _sse_subscribers if q.full()]
        for q in dead:
            _sse_subscribers.remove(q)
        for q in _sse_subscribers:
            q.put_nowait(event)


class _TuiLogHandler(logging.Handler):
    def emit(self, record):
        tui_log(f"[dim]{escape(self.format(record))}[/]")


_WEB_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>CTA</title>
  <meta charset="utf-8">
  <style>
    * { box-sizing: border-box; }
    body { background:#1a1a1a; color:#d4d4d4; font-family:monospace; margin:0; padding:.75rem 1rem; }
    h1 { color:#569cd6; margin:0 0 .5rem; font-size:1rem; }
    #cards { display:flex; flex-wrap:wrap; gap:.5rem; margin-bottom:.75rem; min-height:1rem; }
    .card { border:1px solid #444; padding:.4rem .6rem; min-width:180px; border-radius:3px; font-size:.82rem; }
    .card.active { border-color:#f1c40f; }
    .lbl { font-weight:bold; color:#4ec9b0; margin-bottom:.15rem; }
    .lbl.active { color:#f1c40f; }
    .det { color:#888; }
    #log { background:#111; border:1px solid #333; padding:.5rem .75rem;
           height:calc(100vh - 130px); overflow-y:auto; font-size:.82rem; }
    .row { white-space:pre-wrap; word-break:break-all; line-height:1.4; }
    .ts  { color:#555; margin-right:.4rem; user-select:none; }
  </style>
</head>
<body>
  <h1>CTA — Claude Telegram Agent</h1>
  <div id="cards"></div>
  <div id="log"></div>
  <script>
    const log = document.getElementById('log');
    let pin = true;
    log.addEventListener('scroll', () => {
      pin = log.scrollTop + log.clientHeight >= log.scrollHeight - 30;
    });
    function esc(s) { return s.replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;'); }
    function addLine(ts, text) {
      const d = document.createElement('div');
      d.className = 'row';
      d.innerHTML = '<span class="ts">'+ts+'</span>'+esc(text);
      log.appendChild(d);
      if (pin) log.scrollTop = log.scrollHeight;
    }
    const es = new EventSource('/stream');
    es.onmessage = e => {
      const d = JSON.parse(e.data);
      if (!d.ping) addLine(d.ts, d.text);
    };
    async function tick() {
      try {
        const r = await fetch('/status');
        const d = await r.json();
        const el = document.getElementById('cards');
        if (!d.sessions.length) { el.innerHTML = ''; return; }
        el.innerHTML = d.sessions.map(s => `<div class="card${s.active?' active':''}">
          <div class="lbl${s.active?' active':''}">${esc(s.label)}${s.active?' ⚡':''}</div>
          <div class="det">model: ${esc(s.model)}</div>
          <div class="det">cwd: ${esc(s.cwd)}</div>
          <div class="det">msgs: ${s.msgs}</div></div>`).join('');
      } catch {}
    }
    tick(); setInterval(tick, 2000);
  </script>
</body>
</html>"""


@app.route("/")
def _web_index():
    return _WEB_HTML, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/stream")
def _web_stream():
    q = queue.Queue(maxsize=500)
    with _sse_lock:
        _sse_subscribers.append(q)

    @stream_with_context
    def generate():
        with _tui_lock:
            history = list(_log_entries)
        for ts, text in history:
            yield f"data: {json.dumps({'ts': ts, 'text': _strip_rich(text)})}\n\n"
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield 'data: {"ping":true}\n\n'
        finally:
            with _sse_lock:
                if q in _sse_subscribers:
                    _sse_subscribers.remove(q)

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.route("/status")
def _web_status():
    all_keys = set(user_sessions) | set(msg_counts)
    sessions = []
    for key in sorted(all_keys):
        uid, chat_id = key
        sessions.append({
            "label": chat_labels.get(key, f"{uid}:{chat_id}"),
            "model": user_model.get(key, MODEL),
            "cwd": user_cwd.get(key, DEFAULT_CWD).replace(os.path.expanduser("~"), "~", 1),
            "msgs": msg_counts.get(key, 0),
            "active": key == claude_busy_key,
        })
    return {"model": MODEL, "cwd": DEFAULT_CWD, "sessions": sessions}


# ── Claude CLI ────────────────────────────────────────────────────────────────

def call_claude(prompt: str, cwd: str = None, session_id: str = None, model: str = None,
                max_retries: int = 2, retry_delay: float = 2.0, timeout: int = None) -> tuple[str, str]:
    """Call Claude Code CLI. Returns (text, session_id).

    Serialized with claude_lock because Max/Pro subscriptions only allow
    one concurrent CLI session — a second call would hang or error.
    Retries up to max_retries times on transient failures (empty/error response).
    """
    cwd = cwd or DEFAULT_CWD
    cmd = ["claude", "--print", "--dangerously-skip-permissions",
           "--model", model or MODEL, "--output-format", "json", "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout or TIMEOUT, cwd=cwd)
            if not result.stdout.strip():
                last_error = result.stderr.strip()
            else:
                data = json.loads(result.stdout)
                text = (data.get("result") or "").strip() or "(empty response)"
                return text, data.get("session_id", "")
        except subprocess.TimeoutExpired:
            return "(Claude timed out)", ""
        except FileNotFoundError:
            return "(claude CLI not found — install @anthropic-ai/claude-code)", ""
        if attempt < max_retries:
            tui_log(f"[yellow]⚠ empty response, retrying ({attempt + 1}/{max_retries})…[/]")
            time.sleep(retry_delay)
    return (f"[Error] {last_error}" if last_error else "(empty response)"), ""


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
    global claude_busy_for, claude_busy_key
    key = (uid, chat_id)
    cwd = user_cwd.get(key, DEFAULT_CWD)
    model = user_model.get(key, MODEL)
    timeout = user_timeout.get(key, TIMEOUT)
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

    # Build preamble with agent identity and context files
    memory_path = os.path.join(MEMORY_DIR, f"{uid}:{chat_id}.md")
    crons_path = os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json")
    memory_prefix = (
        f"[Agent chat:{uid}:{chat_id} | memory:{memory_path} | crons:{crons_path}]\n"
        f"Always reply after tool use.\n\n"
    )

    # Build prompt — download document to temp file if present
    caption = message.caption or ""
    prompt = memory_prefix + (message.text or caption)
    tmp_photo = None
    if message.document:
        try:
            mime = message.document.mime_type or ""
            file_ext = os.path.splitext(message.document.file_name or "")[1] or (
                "." + mime.split("/")[-1] if mime else ".bin"
            )
            file_info = bot.get_file(message.document.file_id)
            data = bot.download_file(file_info.file_path)
            ext = os.path.splitext(file_info.file_path)[1] or file_ext
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext, dir=cwd)
            tmp.write(data)
            tmp.close()
            tmp_photo = tmp.name
            user_instruction = f"\n\nUser's question: {caption}" if caption else ""
            prompt = memory_prefix + f"Use the Read tool to read and analyze the file at: {tmp_photo}{user_instruction}"
        except Exception as e:
            tui_log(f"[red]⚠ file download failed: {escape(str(e))}[/]")
            bot.reply_to(message, f"❌ Could not download file: {e}")
            done.set()
            return

    with claude_lock:
        claude_busy_for = username
        claude_busy_key = key
        try:
            reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=session_id, model=model, timeout=timeout)
            if session_id and "No conversation found with session ID" in reply:
                tui_log(f"[yellow]⚠ stale session for {escape(username)}, retrying fresh[/]")
                user_sessions.pop(key, None)
                reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=None, model=model)
        finally:
            claude_busy_for = None
            claude_busy_key = None
            done.set()
            if tmp_photo:
                os.unlink(tmp_photo)

    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[blue]←[/] [bold]{escape(username)}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    for chunk in _split_reply(reply):
        _send_markdown(message, chunk)


def _process_cron(uid: int, chat_id: int, task: dict, done: threading.Event):
    global claude_busy_for, claude_busy_key
    key = (uid, chat_id)
    cwd = user_cwd.get(key, DEFAULT_CWD)
    model = user_model.get(key, MODEL)
    timeout = user_timeout.get(key, TIMEOUT)
    session_id = user_sessions.get(key)
    job_id = task["job_id"]

    memory_path = os.path.join(MEMORY_DIR, f"{uid}:{chat_id}.md")
    crons_path = os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json")
    preamble = (
        f"[Agent chat:{uid}:{chat_id} | memory:{memory_path} | crons:{crons_path}]\n"
        f"Always reply after tool use.\n\n"
    )
    prompt = preamble + f"[Scheduled task {job_id}]\n{task['prompt']}"

    with claude_lock:
        claude_busy_for = f"cron:{job_id}"
        claude_busy_key = key
        try:
            reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=session_id, model=model, timeout=timeout)
            if session_id and "No conversation found with session ID" in reply:
                user_sessions.pop(key, None)
                reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=None, model=model, timeout=timeout)
        finally:
            claude_busy_for = None
            claude_busy_key = None
            done.set()

    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[magenta]←[/] [bold]cron:{job_id}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    for chunk in _split_reply(reply):
        bot.send_message(chat_id, telegramify_markdown.markdownify(chunk), parse_mode="MarkdownV2")


def _user_worker(uid: int, chat_id: int, q: queue.Queue):
    while True:
        item = q.get()
        done = threading.Event()
        threading.Thread(target=_typing_loop, args=(chat_id, done), daemon=True).start()
        try:
            if isinstance(item, dict) and item.get("_type") == "cron":
                _process_cron(uid, chat_id, item, done)
            else:
                _process_message(uid, chat_id, item, done)
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


def cmd_help(message):
    if not _allowed(message): return
    bot.reply_to(message, (
        "*Commands*\n"
        "/help — show this message\n"
        "/start — hello message\n"
        "/clear — reset conversation session\n"
        "/cd `<path>` — change working directory (creates it if needed)\n"
        "/pwd — show current working directory\n"
        "/model `<name>` — switch Claude model (clears session)\n"
        "/timeout `<seconds>` — set per-chat Claude timeout (or `reset`)\n"
        "/status — show model, cwd, timeout, and session info"
    ), parse_mode="Markdown")


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
    created = False
    if not os.path.isdir(expanded):
        try:
            os.makedirs(expanded, exist_ok=True)
            created = True
        except OSError as e:
            bot.reply_to(message, f"❌ Could not create directory: `{e}`", parse_mode="Markdown")
            return
    user_cwd[(uid, message.chat.id)] = expanded
    user_sessions.pop((uid, message.chat.id), None)
    save_sessions()
    suffix = " (created)" if created else ""
    bot.reply_to(message, f"📂 → `{expanded}`{suffix} (session cleared)", parse_mode="Markdown")


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


def cmd_timeout(message):
    if not _allowed(message): return
    uid = message.from_user.id
    key = (uid, message.chat.id)
    val = message.text.replace("/timeout", "", 1).strip()
    if not val:
        current = user_timeout.get(key, TIMEOUT)
        bot.reply_to(message, f"⏱ Timeout: `{current}s`", parse_mode="Markdown")
        return
    if val == "reset":
        user_timeout.pop(key, None)
        bot.reply_to(message, f"⏱ Timeout reset to default (`{TIMEOUT}s`)", parse_mode="Markdown")
        return
    try:
        seconds = int(val)
        if seconds <= 0:
            raise ValueError
    except ValueError:
        bot.reply_to(message, "❌ Usage: `/timeout <seconds>` or `/timeout reset`", parse_mode="Markdown")
        return
    user_timeout[key] = seconds
    bot.reply_to(message, f"⏱ Timeout → `{seconds}s`", parse_mode="Markdown")


def cmd_status(message):
    if not _allowed(message): return
    uid = message.from_user.id
    key = (uid, message.chat.id)
    bot.reply_to(
        message,
        f"🤖 Model: `{user_model.get(key, MODEL)}`\n"
        f"📂 Cwd: `{user_cwd.get(key, DEFAULT_CWD)}`\n"
        f"⏱ Timeout: `{user_timeout.get(key, TIMEOUT)}s`\n"
        f"🔑 Session: `{user_sessions.get(key, 'none')}`",
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


def handle_document(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        source = "[DM]"
    else:
        source = f"[Group: {escape(message.chat.title or str(message.chat.id))}]"
    fname = (message.document and message.document.file_name) or "(no filename)"
    tui_log(f"[green]→[/] {source} [bold]{escape(str(message.from_user.username or uid))}[/] [dim]📎 {escape(fname)}[/]")
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
    bot.message_handler(commands=["help"])(cmd_help)
    bot.message_handler(commands=["clear"])(cmd_clear)
    bot.message_handler(commands=["cd"])(cmd_cd)
    bot.message_handler(commands=["pwd"])(cmd_pwd)
    bot.message_handler(commands=["model"])(cmd_model)
    bot.message_handler(commands=["timeout"])(cmd_timeout)
    bot.message_handler(commands=["status"])(cmd_status)
    bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))(handle_message)
    bot.message_handler(content_types=["document"])(handle_document)
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
    threading.Thread(target=_cron_scheduler, daemon=True).start()
    tui_log(f"[dim]Web UI → http://localhost:{WEB_PORT}/[/]")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, use_reloader=False)
