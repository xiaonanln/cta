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
SESSIONS_PATH = os.path.join(CTA_HOME, "agents.json")
MEMORY_DIR = os.path.join(CTA_HOME, "memory")
CRONS_DIR = os.path.join(CTA_HOME, "crons")
PREAMBLE_DIR = os.path.join(CTA_HOME, "preamble")
GLOBAL_PREAMBLE_PATH = os.path.join(CTA_HOME, "global_preamble.md")
GLOBAL_PREAMBLE = ""

bot = None  # initialized in create_bot()
user_sessions: dict[tuple[int, int], str] = {}  # (uid, chat_id) → Claude session ID
user_cwd: dict[tuple[int, int], str] = {}  # (uid, chat_id) → working directory
user_model: dict[tuple[int, int], str] = {}  # (uid, chat_id) → model override
user_timeout: dict[tuple[int, int], int] = {}  # (uid, chat_id) → timeout override (seconds)
user_queues: dict[tuple[int, int], queue.Queue] = {}
user_queues_lock = threading.Lock()
chat_labels: dict[tuple[int, int], str] = {}   # (uid, chat_id) → "DM" or group name
msg_counts: dict[tuple[int, int], int] = {}    # (uid, chat_id) → messages processed
last_reply: dict[tuple[int, int], str] = {}    # (uid, chat_id) → last reply snippet
claude_lock = threading.Lock()  # serialize Claude CLI calls (Max subscription concurrency limit)
claude_busy_for = None  # username of user currently calling Claude
claude_busy_key = None  # (uid, chat_id) of active session
_current_proc: subprocess.Popen = None  # running Claude subprocess (killable)
_cancelled_keys: set = set()           # keys that had /cancel; suppresses reply
chat_history: dict[tuple[int, int], list] = {}  # (uid, chat_id) → [{role,text,ts}]
_chat_sse: dict[tuple[int, int], list] = {}     # (uid, chat_id) → [Queue]
_chat_sse_lock = threading.Lock()


def _read_global_preamble() -> str:
    try:
        with open(GLOBAL_PREAMBLE_PATH) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


def init(config: dict):
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, MODEL, WEB_PORT, DEFAULT_CWD, GLOBAL_PREAMBLE
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    MODEL = config.get("model", "claude-sonnet-4-6")
    WEB_PORT = config.get("web_port", 17488)
    if config.get("default_cwd"):
        DEFAULT_CWD = os.path.expanduser(config["default_cwd"])
    GLOBAL_PREAMBLE = _read_global_preamble()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(CRONS_DIR, exist_ok=True)
    os.makedirs(PREAMBLE_DIR, exist_ok=True)


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
                if entry.get("model"):
                    user_model[key] = entry["model"]
        tui_log(f"[dim]Loaded {len(data)} session(s) from {SESSIONS_PATH}[/]")
    except Exception as e:
        tui_log(f"[red]Warning: could not load sessions: {escape(str(e))}[/]")


def save_sessions():
    tmp = SESSIONS_PATH + ".tmp"
    try:
        all_keys = set(user_sessions) | set(user_cwd) | set(user_model)
        data = {}
        for key in all_keys:
            uid, chat_id = key
            entry = {}
            if key in user_sessions:
                entry["session"] = user_sessions[key]
            if key in user_cwd:
                entry["cwd"] = user_cwd[key]
            if key in user_model:
                entry["model"] = user_model[key]
            data[f"{uid}:{chat_id}"] = entry
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, SESSIONS_PATH)
    except Exception as e:
        tui_log(f"[red]Warning: could not save sessions: {escape(str(e))}[/]")


# ── Cron scheduler ────────────────────────────────────────────────────────────

def _cron_path(uid: int, chat_id: int) -> str:
    return os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json")


def _preamble_path(uid: int, chat_id: int) -> str:
    return os.path.join(PREAMBLE_DIR, f"{uid}:{chat_id}.md")


def _read_preamble(uid: int, chat_id: int) -> str:
    try:
        with open(_preamble_path(uid, chat_id)) as f:
            return f.read().strip()
    except FileNotFoundError:
        return ""


_CRON_EXAMPLE = [
    {
        "id": "example",
        "schedule": "0 9 * * *",
        "prompt": "Send a good morning message.",
        "next_run": "2099-01-01T09:00:00",
        "_comment": "Edit or remove this example. Schedule uses cron syntax: minute hour day month weekday",
    }
]


def _ensure_cron_file(uid: int, chat_id: int):
    """Write an example cron file if none exists yet, so Claude knows the format."""
    path = _cron_path(uid, chat_id)
    if not os.path.exists(path):
        _save_cron_jobs(uid, chat_id, _CRON_EXAMPLE)


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
    stripped = _strip_rich(text)
    with _tui_lock:
        _log_entries.append((ts, stripped))
    event = {"ts": ts, "text": stripped}
    with _sse_lock:
        _sse_subscribers[:] = [q for q in _sse_subscribers if not q.full()]
        for q in _sse_subscribers:
            q.put_nowait(event)


class _LogHandler(logging.Handler):
    def emit(self, record):
        tui_log(self.format(record))


_WEB_HTML = """<!DOCTYPE html>
<html>
<head>
  <title>CTA</title>
  <meta charset="utf-8">
  <style>
    *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

    /* ── Theme variables ── */
    :root {
      --bg:       #1e1e2e; --bg2:    #181825; --bg3:    #313244;
      --border:   #313244; --border2: #1e1e2e;
      --fg:       #cdd6f4; --fg2:    #6c7086; --fg3:    #45475a;
      --accent:   #89b4fa; --green:  #a6e3a1; --yellow: #f9e2af;
      --preview:  #a6adc8;
    }
    body.light {
      --bg:       #eff1f5; --bg2:    #e6e9ef; --bg3:    #dce0e8;
      --border:   #bcc0cc; --border2: #eff1f5;
      --fg:       #4c4f69; --fg2:    #6c6f85; --fg3:    #8c8fa1;
      --accent:   #1e66f5; --green:  #40a02b; --yellow: #df8e1d;
      --preview:  #5c5f77;
    }

    body { background: var(--bg); color: var(--fg);
           font-family: system-ui, -apple-system, sans-serif;
           display: flex; height: 100vh; overflow: hidden; }

    /* ── Left nav bar ── */
    #nav { width: 260px; flex-shrink: 0; background: var(--bg2);
           border-right: 1px solid var(--border);
           display: flex; flex-direction: column; }

    #nav-logo { padding: 1.1rem 1.25rem;
                font-size: 1.1rem; font-weight: 700;
                color: var(--accent); letter-spacing: .04em;
                border-bottom: 1px solid var(--border); flex-shrink: 0; }
    #nav-logo span { font-weight: 400; color: var(--fg2); font-size: .8rem;
                     display: block; margin-top: .2rem; }

    #nav-items { padding: .6rem; flex-shrink: 0; }
    .nav-item { display: flex; align-items: center; gap: .7rem;
                padding: .7rem .85rem; border-radius: 7px;
                font-size: .95rem; font-weight: 500; color: var(--fg2);
                cursor: pointer; transition: background .12s, color .12s;
                user-select: none; }
    .nav-item:hover { background: var(--bg3); color: var(--fg); }
    .nav-item.sel { background: var(--bg3); color: var(--accent); }
    .nav-icon { font-size: 1.05rem; width: 1.3rem; text-align: center; flex-shrink: 0; }

    .pulse { width: 7px; height: 7px; border-radius: 50%; flex-shrink: 0;
             background: var(--green); margin-top: .3rem;
             animation: blink 1.2s ease-in-out infinite; }
    @keyframes blink { 0%,100%{opacity:1} 50%{opacity:.15} }

    /* ── Right main content ── */
    #main { flex: 1; display: flex; flex-direction: column; overflow: hidden; }

    /* shared topbar */
    .topbar { padding: .5rem 1rem; border-bottom: 1px solid var(--border);
              font-size: .8rem; font-weight: 600; color: var(--fg);
              flex-shrink: 0; display: flex; align-items: center; gap: .6rem; }
    .topbar-sub { font-weight: 400; color: var(--fg2); font-size: .72rem; }
    #theme-btn { margin-left: auto; background: none; border: 1px solid var(--border);
                 border-radius: 6px; padding: .2rem .55rem; cursor: pointer;
                 font-size: .85rem; color: var(--fg2); transition: background .12s; }
    #theme-btn:hover { background: var(--bg3); }

    /* views */
    .view { flex: 1; overflow-y: auto; display: none; }
    .view.sel { display: flex; flex-direction: column; }

    /* log view */
    #v-log { padding: .65rem 1rem;
             font-family: "SF Mono", "Fira Code", "Consolas", monospace;
             font-size: .78rem; line-height: 1.55; }
    .log-row { display: flex; gap: .55rem; }
    .ts { color: var(--fg3); flex-shrink: 0; width: 5.5rem; user-select: none; }
    .txt { flex: 1; white-space: pre-wrap; word-break: break-word; }

    /* chats view */
    #v-chats { display: flex; flex-wrap: wrap; padding: 1.25rem; gap: 1rem;
               align-content: flex-start; }
    .chat-card { background: var(--bg2); border: 1px solid var(--border);
                 border-radius: 8px; padding: 1rem 1.1rem; width: 300px; }
    .chat-card.busy { border-color: var(--green); }
    .cc-name { font-size: .95rem; font-weight: 600;
               display: flex; align-items: center; gap: .45rem; margin-bottom: .6rem; }
    .cc-row { display: flex; justify-content: space-between;
              font-size: .75rem; padding: .2rem 0; }
    .cc-key { color: var(--fg2); }
    .cc-val { color: var(--fg); font-family: monospace; font-size: .72rem;
              max-width: 180px; overflow: hidden;
              text-overflow: ellipsis; white-space: nowrap; text-align: right; }
    .cc-preview { margin-top: .65rem; padding-top: .6rem;
                  border-top: 1px solid var(--border);
                  font-size: .75rem; color: var(--preview); line-height: 1.55;
                  display: -webkit-box; -webkit-line-clamp: 4;
                  -webkit-box-orient: vertical; overflow: hidden; }
    .cc-preamble-label { margin-top: .75rem; padding-top: .6rem;
                         border-top: 1px solid var(--border);
                         font-size: .7rem; color: var(--fg2); margin-bottom: .3rem; }
    .cc-preamble { width: 100%; background: var(--bg); border: 1px solid var(--border);
                   border-radius: 4px; color: var(--fg); font-family: inherit;
                   font-size: .75rem; padding: .4rem .5rem; resize: vertical;
                   min-height: 60px; outline: none; }
    .cc-preamble:focus { border-color: var(--accent); }
    .cc-save { margin-top: .35rem; padding: .3rem .7rem;
               background: var(--bg3); border: none; border-radius: 4px;
               color: var(--fg); font-size: .72rem; cursor: pointer; }
    .cc-save:hover { background: var(--border); }
    .cc-saved { color: var(--green); font-size: .7rem; margin-left: .4rem;
                opacity: 0; transition: opacity .3s; }
    .cc-open { margin-top: .35rem; padding: .3rem .7rem;
               background: none; border: 1px solid var(--accent); border-radius: 4px;
               color: var(--accent); font-size: .72rem; cursor: pointer; }
    .cc-open:hover { background: var(--accent); color: var(--bg); }
    #no-cards { padding: 1rem; font-size: .82rem; color: var(--fg3); }

    /* crons view */
    #v-crons { padding: 1.25rem; flex-direction: column; gap: .75rem; }
    .crons-table { width: 100%; border-collapse: collapse; font-size: .78rem; }
    .crons-table th { text-align: left; color: var(--fg2); font-weight: 600;
                      padding: .4rem .75rem; border-bottom: 1px solid var(--border); }
    .crons-table td { padding: .45rem .75rem; border-bottom: 1px solid var(--border2);
                      vertical-align: top; }
    .crons-table tr:hover td { background: var(--bg2); }
    .crons-table tr.example td { color: var(--fg3); font-style: italic; }
    .cron-prompt { color: var(--preview); white-space: pre-wrap; word-break: break-word; }
    .cron-chat { color: var(--accent); white-space: nowrap; }
    .cron-id { color: var(--fg); white-space: nowrap; }
    .cron-sched { font-family: monospace; color: var(--green); white-space: nowrap; }
    .cron-next { color: var(--fg2); white-space: nowrap; font-size: .72rem; }
    .cron-del { background: none; border: 1px solid var(--border); border-radius: 4px;
                color: var(--fg2); font-size: .7rem; padding: .15rem .45rem; cursor: pointer; }
    .cron-del:hover { background: #e64553; color: #fff; border-color: #e64553; }
    #no-crons { color: var(--fg3); font-size: .82rem; padding: .5rem 0; }

    /* add-cron form */
    .cron-form { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
                 padding: 1rem 1.1rem; margin-bottom: 1rem; }
    .cron-form h3 { font-size: .85rem; font-weight: 600; margin-bottom: .75rem; color: var(--accent); }
    .cron-form-row { display: flex; gap: .6rem; align-items: center; margin-bottom: .55rem;
                     font-size: .8rem; }
    .cron-form-row label { color: var(--fg2); width: 70px; flex-shrink: 0; font-size: .75rem; }
    .cron-form-row input, .cron-form-row select, .cron-form-row textarea {
      flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 4px;
      color: var(--fg); font-family: inherit; font-size: .78rem; padding: .35rem .5rem; outline: none; }
    .cron-form-row input:focus, .cron-form-row select:focus, .cron-form-row textarea:focus {
      border-color: var(--accent); }
    .cron-form-row textarea { resize: vertical; min-height: 50px; }
    .cron-form-actions { display: flex; gap: .5rem; align-items: center; margin-top: .4rem; }
    .cron-form-btn { padding: .35rem .85rem; background: var(--accent); border: none;
                     border-radius: 5px; color: #fff; font-size: .78rem; font-weight: 600;
                     cursor: pointer; }
    .cron-form-btn:hover { opacity: .85; }
    .cron-form-btn:disabled { opacity: .4; cursor: default; }
    .cron-form-msg { font-size: .72rem; margin-left: .4rem; }
    .cron-form-msg.ok { color: var(--green); }
    .cron-form-msg.err { color: #e64553; }

    /* chat view (embedded) */
    #view-chat { flex-direction: column; }
    #chat-messages { flex: 1; overflow-y: auto; padding: 1rem; display: flex;
                     flex-direction: column; gap: .6rem; }
    .cmsg { max-width: 75%; padding: .55rem .8rem; border-radius: 12px; line-height: 1.45;
            white-space: pre-wrap; word-break: break-word; }
    .cmsg.user { align-self: flex-end; background: var(--accent); color: #fff;
                 border-bottom-right-radius: 3px; }
    .cmsg.assistant { align-self: flex-start; background: var(--bg2); color: var(--fg);
                      border-bottom-left-radius: 3px; }
    .cmsg.cron { align-self: flex-start; background: var(--bg3); color: var(--fg2);
                 border-bottom-left-radius: 3px; font-size: .78rem; }
    .cmsg-meta { font-size: .68rem; color: var(--fg3); margin-top: .2rem; }
    .cmsg.user .cmsg-meta { text-align: right; }
    #chat-inputbar { padding: .65rem 1rem; background: var(--bg2);
                     border-top: 1px solid var(--border);
                     display: flex; gap: .5rem; flex-shrink: 0; }
    #chat-inp { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
                padding: .45rem .75rem; color: var(--fg); font-size: .85rem; font-family: inherit;
                resize: none; min-height: 38px; max-height: 150px; outline: none; }
    #chat-inp:focus { border-color: var(--accent); }
    #chat-send { background: var(--accent); color: #fff; border: none; border-radius: 8px;
                 padding: .45rem .9rem; cursor: pointer; font-size: .85rem; font-weight: 600; }
    #chat-send:hover { opacity: .85; }
    #chat-send:disabled { opacity: .4; cursor: default; }
    #chat-typing { align-self: flex-start; padding: .45rem .8rem; display: none; }
    #chat-typing.show { display: flex; }
    .typing-dots { display: flex; gap: 4px; align-items: center; }
    .typing-dots span { width: 7px; height: 7px; border-radius: 50%; background: var(--fg3);
                        animation: typebounce .9s ease-in-out infinite; }
    .typing-dots span:nth-child(2) { animation-delay: .15s; }
    .typing-dots span:nth-child(3) { animation-delay: .3s; }
    @keyframes typebounce { 0%,60%,100%{transform:translateY(0)} 30%{transform:translateY(-5px)} }

    /* status view */
    #v-status { padding: 1.25rem; align-content: flex-start; }
    .stat-block { background: var(--bg2); border: 1px solid var(--border);
                  border-radius: 8px; padding: .85rem 1rem;
                  max-width: 400px; width: 100%; }
    .stat-row { display: flex; justify-content: space-between;
                font-size: .8rem; padding: .3rem 0;
                border-bottom: 1px solid var(--border); }
    .stat-row:last-child { border-bottom: none; }
    .stat-key { color: var(--fg2); }
    .stat-val { color: var(--fg); font-family: monospace; font-size: .75rem;
                text-align: right; max-width: 250px;
                overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
    /* config view */
    #view-config { padding: 1.25rem; }
    .cfg-form { background: var(--bg2); border: 1px solid var(--border); border-radius: 8px;
                padding: 1rem 1.25rem; max-width: 480px; }
    .cfg-form h3 { font-size: .82rem; font-weight: 600; color: var(--accent);
                   margin-bottom: .9rem; }
    .cfg-row { display: flex; align-items: center; gap: .6rem; margin-bottom: .6rem; }
    .cfg-row label { color: var(--fg2); font-size: .75rem; width: 130px; flex-shrink: 0; }
    .cfg-row input, .cfg-row select { flex: 1; background: var(--bg); border: 1px solid var(--border);
                     border-radius: 6px; padding: .35rem .65rem; color: var(--fg);
                     font-size: .8rem; font-family: monospace; outline: none; }
    .cfg-row input:focus, .cfg-row select:focus { border-color: var(--accent); }
    .cfg-row input[type=number] { width: 90px; flex: none; }
    .cfg-actions { display: flex; align-items: center; gap: .6rem; margin-top: .75rem; }
    .cfg-save-btn { padding: .35rem .85rem; background: var(--accent); border: none;
                    border-radius: 6px; color: #fff; font-size: .8rem;
                    font-weight: 600; cursor: pointer; }
    .cfg-save-btn:hover { opacity: .85; }
    .cfg-msg { font-size: .72rem; }
    .cfg-msg.ok { color: var(--green); }
    .cfg-msg.err { color: #e64553; }
    .cfg-note { font-size: .68rem; color: var(--fg3); margin-top: .65rem; }
  </style>
</head>
<body class="light">

<div id="nav">
  <div id="nav-logo">CTA<span id="gmodel">—</span></div>

  <div id="nav-items">
    <div class="nav-item sel" data-view="chats">
      <span class="nav-icon">💬</span> Chats
    </div>
    <div class="nav-item" data-view="crons">
      <span class="nav-icon">⏰</span> Cronjobs
    </div>
    <div class="nav-item" data-view="log">
      <span class="nav-icon">📋</span> Log
    </div>
    <div class="nav-item" data-view="status">
      <span class="nav-icon">⚙️</span> Status
    </div>
    <div class="nav-item" data-view="config">
      <span class="nav-icon">🔧</span> Config
    </div>
  </div>

</div>

<div id="main">
  <div class="topbar" id="topbar-label">
    <span>Log</span>
    <span class="topbar-sub" id="topbar-sub"></span>
    <button id="theme-btn" onclick="toggleTheme()">&#9728;</button>
  </div>

  <!-- Chats view -->
  <div class="view sel" id="view-chats">
    <div id="v-chats"><div id="no-cards">No active chats yet</div></div>
  </div>

  <!-- Crons view -->
  <div class="view" id="view-crons">
    <div id="v-crons">
      <div class="cron-form" id="cron-form">
        <h3>Add Cronjob</h3>
        <div class="cron-form-row">
          <label>Chat</label>
          <select id="cron-chat-sel"><option value="">Loading…</option></select>
        </div>
        <div class="cron-form-row">
          <label>Job ID</label>
          <input id="cron-id-inp" placeholder="e.g. morning-report" />
        </div>
        <div class="cron-form-row">
          <label>Schedule</label>
          <input id="cron-sched-inp" placeholder="minute hour day month weekday  e.g. 0 9 * * *" />
        </div>
        <div class="cron-form-row">
          <label>Prompt</label>
          <textarea id="cron-prompt-inp" placeholder="What should the agent do?"></textarea>
        </div>
        <div class="cron-form-actions">
          <button class="cron-form-btn" onclick="addCron()">Add</button>
          <span class="cron-form-msg" id="cron-form-msg"></span>
        </div>
      </div>
      <div id="cron-table-area"></div>
    </div>
  </div>

  <!-- Chat view (inline) -->
  <div class="view" id="view-chat">
    <div id="chat-messages">
      <div id="chat-typing"><div class="typing-dots"><span></span><span></span><span></span></div></div>
    </div>
    <div id="chat-inputbar">
      <textarea id="chat-inp" rows="3" placeholder="Message… (Ctrl+Enter to send)"></textarea>
      <button id="chat-send" onclick="chatSend()">Send</button>
    </div>
  </div>

  <!-- Log view -->
  <div class="view" id="view-log">
    <div id="v-log"></div>
  </div>

  <!-- Status view -->
  <div class="view" id="view-status">
    <div id="v-status">
      <div class="stat-block" id="stat-block"></div>
    </div>
  </div>

  <!-- Config view -->
  <div class="view" id="view-config">
    <div class="cfg-form">
      <h3>Global Configuration</h3>
      <div class="cfg-row">
        <label>Telegram bot token</label>
        <input id="cfg-token" type="text" />
      </div>
      <div class="cfg-row">
        <label>Default model</label>
        <select id="cfg-model">
          <option value="claude-opus-4-6">claude-opus-4-6</option>
          <option value="claude-sonnet-4-6">claude-sonnet-4-6</option>
          <option value="claude-haiku-4-5-20251001">claude-haiku-4-5-20251001</option>
        </select>
      </div>
      <div class="cfg-row">
        <label>Claude timeout (s)</label>
        <input id="cfg-timeout" type="number" min="10" />
      </div>
      <div class="cfg-row">
        <label>Web port</label>
        <input id="cfg-port" type="number" min="1024" max="65535" />
      </div>
      <div class="cfg-row">
        <label>Default cwd</label>
        <input id="cfg-cwd" type="text" />
      </div>
      <div class="cfg-row">
        <label>Allowed users</label>
        <input id="cfg-users" type="text" placeholder="comma-separated user IDs, empty = all" />
      </div>
      <div class="cfg-row" style="flex-direction:column;align-items:flex-start;gap:0.4rem;">
        <label>Global preamble</label>
        <textarea id="cfg-global-preamble" rows="5" style="width:100%;box-sizing:border-box;resize:vertical;font-family:inherit;font-size:0.9rem;background:var(--bg);color:var(--fg);border:1px solid var(--border);border-radius:4px;padding:0.4rem;" placeholder="Instructions injected into every agent's preamble…"></textarea>
      </div>
      <div class="cfg-actions">
        <button class="cfg-save-btn" onclick="saveConfig()">Save</button>
        <span class="cfg-msg" id="cfg-msg"></span>
      </div>
      <div class="cfg-note">Bot token and web port changes require a restart to take effect.</div>
    </div>
  </div>
</div>

<script>
  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── Nav switching ──
  let currentView = 'chats';
  let chatUid = null, chatChatId = null, chatES = null;
  const VIEW_LABELS = { chats: 'Chats', crons: 'Cronjobs', log: 'Log', status: 'Status', config: 'Config' };

  function viewFromHash() {
    const h = location.hash.replace(/^#/, '');
    return VIEW_LABELS[h] ? h : 'chats';
  }

  let _popNav = false;

  function selectView(name) {
    currentView = name;
    // Close chat SSE when navigating away
    if (name !== 'chat' && chatES) { chatES.close(); chatES = null; }
    document.querySelectorAll('.nav-item').forEach(el => {
      el.classList.toggle('sel', el.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach(el => {
      el.classList.toggle('sel', el.id === 'view-' + name);
    });
    document.getElementById('topbar-label').firstElementChild.textContent = VIEW_LABELS[name] || name;
    document.getElementById('topbar-sub').textContent = '';
    if (name === 'config') loadConfig();
    if (!_popNav) {
      const hash = name === 'chats' ? '' : '#' + name;
      history.pushState({view: name}, '', location.pathname + hash);
    }
  }

  window.addEventListener('popstate', e => {
    _popNav = true;
    selectView((e.state && e.state.view) || viewFromHash());
    _popNav = false;
  });

  document.querySelectorAll('.nav-item').forEach(el => {
    el.addEventListener('click', () => selectView(el.dataset.view));
  });

  // ── Live log ──
  const logEl = document.getElementById('v-log');
  let pin = true;
  document.getElementById('view-log').addEventListener('scroll', e => {
    pin = e.target.scrollTop + e.target.clientHeight >= e.target.scrollHeight - 40;
  });

  function addLine(ts, text) {
    const row = document.createElement('div');
    row.className = 'log-row';
    row.innerHTML = `<span class="ts">${esc(ts)}</span><span class="txt">${esc(text)}</span>`;
    logEl.appendChild(row);
    const logView = document.getElementById('view-log');
    if (pin) logView.scrollTop = logView.scrollHeight;
  }

  const es = new EventSource('/stream');
  es.onmessage = e => {
    const d = JSON.parse(e.data);
    if (!d.ping) addLine(d.ts, d.text);
  };

  // ── Preamble ──
  // Track user edits locally so tick() rebuilds don't overwrite in-progress typing
  const localPreambles = new Map(); // "uid:chat_id" -> current textarea value

  document.addEventListener('input', e => {
    if (e.target.classList.contains('cc-preamble')) {
      const key = `${e.target.dataset.uid}:${e.target.dataset.chat}`;
      localPreambles.set(key, e.target.value);
    }
  });

  async function savePreamble(btn) {
    const card = btn.closest('.chat-card');
    const ta = card.querySelector('.cc-preamble');
    const saved = card.querySelector('.cc-saved');
    const uid = ta.dataset.uid, chat_id = ta.dataset.chat;
    const key = `${uid}:${chat_id}`;
    await fetch(`/preamble/${uid}/${chat_id}`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({preamble: ta.value}),
    });
    localPreambles.delete(key); // synced with server, no longer need local copy
    saved.style.opacity = 1;
    setTimeout(() => saved.style.opacity = 0, 1500);
  }

  // ── Status polling ──
  async function tick() {
    try {
      const r = await fetch('/status');
      const d = await r.json();

      document.getElementById('gmodel').textContent = d.model;

      // Chats view — cards
      const chatsEl = document.getElementById('v-chats');
      if (!d.sessions.length) {
        chatsEl.innerHTML = '<div id="no-cards">No active chats yet</div>';
      } else {
        chatsEl.innerHTML = d.sessions.map(s => {
          const key = `${s.uid}:${s.chat_id}`;
          const preambleVal = localPreambles.has(key) ? localPreambles.get(key) : s.preamble;
          return `
          <div class="chat-card${s.active ? ' busy' : ''}">
            <div class="cc-name">
              ${s.active ? '<span class="pulse"></span>' : ''}
              ${esc(s.label)}
            </div>
            <div class="cc-row"><span class="cc-key">model</span><span class="cc-val" title="${esc(s.model)}">${esc(s.model)}</span></div>
            <div class="cc-row"><span class="cc-key">cwd</span><span class="cc-val" title="${esc(s.cwd)}">${esc(s.cwd)}</span></div>
            <div class="cc-row"><span class="cc-key">msgs</span><span class="cc-val">${s.msgs}</span></div>
            ${s.last_reply ? `<div class="cc-preview">${esc(s.last_reply)}</div>` : ''}
            <div class="cc-preamble-label">Custom preamble</div>
            <textarea class="cc-preamble" data-uid="${s.uid}" data-chat="${s.chat_id}">${esc(preambleVal)}</textarea>
            <div style="display:flex;gap:.4rem;align-items:center">
              <button class="cc-save" onclick="savePreamble(this)">Save preamble</button>
              <span class="cc-saved">Saved ✓</span>
              <button class="cc-open" onclick="openChat(${s.uid},${s.chat_id},'${esc(s.label).replace(/'/g,"\\'")}')">Open chat</button>
            </div>
          </div>`;
        }).join('');
      }

      // Crons view — table only (form is static HTML)
      const tableArea = document.getElementById('cron-table-area');
      try {
        const cr = await fetch('/cronjobs');
        const cd = await cr.json();
        if (!cd.jobs.length) {
          tableArea.innerHTML = '<div id="no-crons">No cron jobs yet</div>';
        } else {
          tableArea.innerHTML = `<table class="crons-table">
            <thead><tr>
              <th>Chat</th><th>ID</th><th>Schedule</th><th>Next run</th><th>Prompt</th><th></th>
            </tr></thead>
            <tbody>${cd.jobs.map(j => `
              <tr class="${j.example ? 'example' : ''}">
                <td class="cron-chat">${esc(j.chat)}</td>
                <td class="cron-id">${esc(j.id)}</td>
                <td class="cron-sched">${esc(j.schedule)}</td>
                <td class="cron-next">${esc(j.next_run.replace('T',' ').slice(0,16))}</td>
                <td class="cron-prompt">${esc(j.prompt)}</td>
                <td><button class="cron-del" onclick="delCron(${j.uid},${j.chat_id},'${esc(j.id)}')">✕</button></td>
              </tr>`).join('')}
            </tbody></table>`;
        }
      } catch {}

      // Load chat options for cron form (only once)
      if (!cronChatsLoaded) loadCronChats();

      // Status view
      document.getElementById('stat-block').innerHTML = [
        ['Model',    d.model],
        ['Default cwd', d.cwd],
        ['Sessions', d.sessions.length],
        ['Active',   d.sessions.filter(s => s.active).map(s => s.label).join(', ') || '—'],
      ].map(([k, v]) => `
        <div class="stat-row">
          <span class="stat-key">${k}</span>
          <span class="stat-val" title="${esc(String(v))}">${esc(String(v))}</span>
        </div>`).join('');

      // Chat typing indicator
      if (currentView === 'chat' && chatUid !== null) {
        const isActive = d.sessions.some(s => s.uid === chatUid && s.chat_id === chatChatId && s.active);
        const el = document.getElementById('chat-typing');
        el.classList.toggle('show', isActive);
        if (isActive) el.scrollIntoView({behavior: 'smooth'});
      }

    } catch {}
  }
  // On page load: show the view matching the hash, replace (not push) history entry
  history.replaceState({view: viewFromHash()}, '', location.href);
  _popNav = true; selectView(viewFromHash()); _popNav = false;
  tick();
  setInterval(tick, 2000);

  // ── Cron CRUD ──
  let cronChatsLoaded = false;
  async function loadCronChats() {
    try {
      const r = await fetch('/chats');
      const d = await r.json();
      const sel = document.getElementById('cron-chat-sel');
      if (!d.chats.length) {
        sel.innerHTML = '<option value="">No chats available</option>';
        return;
      }
      sel.innerHTML = '<option value="">Select a chat…</option>' +
        d.chats.map(c => `<option value="${c.uid}:${c.chat_id}">${esc(c.label)}${c.cwd ? ' — ' + esc(c.cwd.replace(/.*\//, '')) : ''}</option>`).join('');
      cronChatsLoaded = true;
    } catch {}
  }

  async function addCron() {
    const msg = document.getElementById('cron-form-msg');
    const sel = document.getElementById('cron-chat-sel').value;
    const id = document.getElementById('cron-id-inp').value.trim();
    const schedule = document.getElementById('cron-sched-inp').value.trim();
    const prompt = document.getElementById('cron-prompt-inp').value.trim();
    if (!sel) { showMsg(msg, 'Select a chat', true); return; }
    if (!id) { showMsg(msg, 'Enter a job ID', true); return; }
    if (!schedule) { showMsg(msg, 'Enter a cron schedule', true); return; }
    if (!prompt) { showMsg(msg, 'Enter a prompt', true); return; }
    const [uid, chat_id] = sel.split(':');
    try {
      const r = await fetch('/cronjobs', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({uid: +uid, chat_id: +chat_id, id, schedule, prompt}),
      });
      const d = await r.json();
      if (!r.ok) { showMsg(msg, d.error || 'Failed', true); return; }
      showMsg(msg, 'Added! Next run: ' + d.next_run.replace('T',' ').slice(0,16), false);
      document.getElementById('cron-id-inp').value = '';
      document.getElementById('cron-sched-inp').value = '';
      document.getElementById('cron-prompt-inp').value = '';
      tick();
    } catch (e) { showMsg(msg, 'Network error', true); }
  }

  async function delCron(uid, chatId, jobId) {
    if (!confirm('Delete cronjob "' + jobId + '"?')) return;
    try {
      await fetch(`/cronjobs/${uid}/${chatId}/${jobId}`, {method: 'DELETE'});
      tick();
    } catch {}
  }

  function showMsg(el, text, isErr) {
    el.textContent = text;
    el.className = 'cron-form-msg ' + (isErr ? 'err' : 'ok');
    setTimeout(() => { el.textContent = ''; }, 3000);
  }

  // ── Config ──
  async function loadConfig() {
    try {
      const r = await fetch('/config');
      const d = await r.json();
      document.getElementById('cfg-token').value = d.telegram_bot_token || '';
      const modelSel = document.getElementById('cfg-model');
      if (d.model && [...modelSel.options].some(o => o.value === d.model)) {
        modelSel.value = d.model;
      }
      document.getElementById('cfg-timeout').value = d.claude_timeout ?? '';
      document.getElementById('cfg-port').value = d.web_port ?? '';
      document.getElementById('cfg-cwd').value = d.default_cwd || '';
      document.getElementById('cfg-users').value = (d.allowed_users || []).join(', ');
      document.getElementById('cfg-global-preamble').value = d.global_preamble || '';
    } catch {}
  }

  async function saveConfig() {
    const msg = document.getElementById('cfg-msg');
    const token = document.getElementById('cfg-token').value.trim();
    const body = {
      model: document.getElementById('cfg-model').value,
      claude_timeout: parseInt(document.getElementById('cfg-timeout').value) || 600,
      web_port: parseInt(document.getElementById('cfg-port').value) || 17488,
      default_cwd: document.getElementById('cfg-cwd').value.trim(),
      allowed_users: document.getElementById('cfg-users').value
        .split(',').map(s => s.trim()).filter(Boolean).map(Number),
      global_preamble: document.getElementById('cfg-global-preamble').value,
    };
    if (token) body.telegram_bot_token = token;
    try {
      const r = await fetch('/config', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      const d = await r.json();
      if (!r.ok) { msg.textContent = d.error || 'Failed'; msg.className = 'cfg-msg err'; }
      else { msg.textContent = 'Saved ✓'; msg.className = 'cfg-msg ok'; }
      setTimeout(() => { msg.textContent = ''; }, 2500);
    } catch { msg.textContent = 'Network error'; msg.className = 'cfg-msg err'; }
  }


  // ── Inline chat ──
  function openChat(uid, chatId, label) {
    // Clean up previous chat SSE
    if (chatES) { chatES.close(); chatES = null; }
    chatUid = uid; chatChatId = chatId;

    // Update topbar and switch view
    document.getElementById('topbar-label').firstElementChild.textContent = label;
    document.getElementById('topbar-sub').textContent = '';
    // Deselect nav items (chat isn't in the nav)
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('sel'));
    document.querySelectorAll('.view').forEach(el => el.classList.remove('sel'));
    document.getElementById('view-chat').classList.add('sel');
    currentView = 'chat';

    // Load history + subscribe
    const msgEl = document.getElementById('chat-messages');
    const typing = document.getElementById('chat-typing');
    // Clear all messages but keep typing indicator
    while (msgEl.firstChild !== typing) msgEl.removeChild(msgEl.firstChild);
    typing.classList.remove('show');
    fetch(`/chat/${uid}/${chatId}/history`).then(r => r.json()).then(d => {
      d.messages.forEach(m => chatAppend(m.role, m.text, m.ts, false));
      msgEl.scrollTop = msgEl.scrollHeight;
    }).catch(() => {});

    chatES = new EventSource(`/chat/${uid}/${chatId}/stream`);
    chatES.onmessage = e => {
      const d = JSON.parse(e.data);
      if (!d.ping) chatAppend(d.role, d.text, d.ts);
    };

    document.getElementById('chat-inp').focus();
  }

  function chatAppend(role, text, ts, scroll=true) {
    const el = document.createElement('div');
    el.className = 'cmsg ' + role;
    const t = new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
    el.innerHTML = esc(text) + `<div class="cmsg-meta">${t}</div>`;
    const c = document.getElementById('chat-messages');
    const typing = document.getElementById('chat-typing');
    c.insertBefore(el, typing);
    if (scroll) el.scrollIntoView({behavior: 'smooth'});
  }

  async function chatSend() {
    if (!chatUid) return;
    const inp = document.getElementById('chat-inp');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = ''; inp.style.height = '';
    document.getElementById('chat-send').disabled = true;
    try {
      await fetch(`/chat/${chatUid}/${chatChatId}/send`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text}),
      });
    } catch {}
    document.getElementById('chat-send').disabled = false;
    inp.focus();
  }

  document.getElementById('chat-inp').addEventListener('keydown', e => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); chatSend(); }
  });
  document.getElementById('chat-inp').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 150) + 'px';
  });

  // ── Theme toggle ──
  function toggleTheme() {
    const light = document.body.classList.toggle('light');
    localStorage.setItem('theme', light ? 'light' : 'dark');
    document.getElementById('theme-btn').innerHTML = light ? '&#9728;' : '&#9790;';
  }
  if (localStorage.getItem('theme') === 'dark') {
    document.body.classList.remove('light');
    document.getElementById('theme-btn').innerHTML = '&#9790;';
  }
</script>
</body>
</html>"""


@app.route("/")
def _web_index():
    return _WEB_HTML, 200, {"Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache"}


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
            yield f"data: {json.dumps({'ts': ts, 'text': text})}\n\n"
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
            "uid": uid,
            "chat_id": chat_id,
            "model": user_model.get(key, MODEL),
            "cwd": user_cwd.get(key, DEFAULT_CWD).replace(os.path.expanduser("~"), "~", 1),
            "msgs": msg_counts.get(key, 0),
            "active": key == claude_busy_key,
            "last_reply": last_reply.get(key, ""),
            "preamble": _read_preamble(uid, chat_id),
        })
    return {"model": MODEL, "cwd": DEFAULT_CWD, "sessions": sessions}


@app.route("/config", methods=["GET"])
def _web_get_config():
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {
        "telegram_bot_token": cfg.get("telegram_bot_token", BOT_TOKEN),
        "model": cfg.get("model", MODEL),
        "claude_timeout": cfg.get("claude_timeout", TIMEOUT),
        "web_port": cfg.get("web_port", WEB_PORT),
        "default_cwd": cfg.get("default_cwd", DEFAULT_CWD),
        "allowed_users": cfg.get("allowed_users", list(ALLOWED_USERS)),
        "global_preamble": _read_global_preamble(),
    }


@app.route("/config", methods=["POST"])
def _web_set_config():
    global BOT_TOKEN, MODEL, TIMEOUT, DEFAULT_CWD, ALLOWED_USERS, GLOBAL_PREAMBLE
    from flask import request
    data = request.get_json(silent=True) or {}
    try:
        with open(CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if data.get("telegram_bot_token"):
        cfg["telegram_bot_token"] = data["telegram_bot_token"]
        BOT_TOKEN = data["telegram_bot_token"]
    if "model" in data and data["model"]:
        cfg["model"] = data["model"]
        MODEL = data["model"]
    if "claude_timeout" in data and data["claude_timeout"] > 0:
        cfg["claude_timeout"] = data["claude_timeout"]
        TIMEOUT = data["claude_timeout"]
    if "web_port" in data and data["web_port"]:
        cfg["web_port"] = data["web_port"]
    if "default_cwd" in data and data["default_cwd"]:
        expanded = os.path.expanduser(data["default_cwd"])
        cfg["default_cwd"] = expanded
        DEFAULT_CWD = expanded
    if "allowed_users" in data:
        cfg["allowed_users"] = [int(u) for u in data["allowed_users"] if str(u).strip()]
        ALLOWED_USERS = set(cfg["allowed_users"])
    if "global_preamble" in data:
        text = data["global_preamble"].strip()
        if text:
            with open(GLOBAL_PREAMBLE_PATH, "w") as f:
                f.write(text)
        else:
            try:
                os.unlink(GLOBAL_PREAMBLE_PATH)
            except FileNotFoundError:
                pass
        GLOBAL_PREAMBLE = text
    try:
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, CONFIG_PATH)
    except Exception as e:
        return {"error": str(e)}, 500
    tui_log(f"[cyan]⚙ config updated via web[/]")
    return {"ok": True}


@app.route("/preamble/<int:uid>/<int:chat_id>", methods=["GET"])
def _web_get_preamble(uid, chat_id):
    return {"preamble": _read_preamble(uid, chat_id)}


@app.route("/preamble/<int:uid>/<int:chat_id>", methods=["POST"])
def _web_set_preamble(uid, chat_id):
    from flask import request
    text = (request.get_json(silent=True) or {}).get("preamble", "").strip()
    path = _preamble_path(uid, chat_id)
    if text:
        with open(path, "w") as f:
            f.write(text)
        tui_log(f"[cyan]📝 preamble updated for {uid}:{chat_id}[/]")
    else:
        try:
            os.unlink(path)
            tui_log(f"[cyan]📝 preamble cleared for {uid}:{chat_id}[/]")
        except FileNotFoundError:
            pass
    return {"ok": True}


@app.route("/cronjobs")
def _web_cronjobs():
    jobs = []
    if os.path.isdir(CRONS_DIR):
        for fname in sorted(os.listdir(CRONS_DIR)):
            if not fname.endswith(".json"):
                continue
            try:
                uid_str, chat_str = fname[:-5].split(":", 1)
                uid, chat_id = int(uid_str), int(chat_str)
            except ValueError:
                continue
            key = (uid, chat_id)
            label = chat_labels.get(key, f"{uid}:{chat_id}")
            for job in _load_cron_jobs(uid, chat_id):
                next_run = job.get("next_run", "")
                if next_run and next_run >= "2099":
                    continue
                jobs.append({
                    "chat": label,
                    "uid": uid,
                    "chat_id": chat_id,
                    "id": job.get("id", ""),
                    "schedule": job.get("schedule", ""),
                    "next_run": job.get("next_run", ""),
                    "prompt": job.get("prompt", ""),
                    "example": job.get("id") == "example",
                })
    return {"jobs": jobs}


@app.route("/cronjobs", methods=["POST"])
def _web_create_cronjob():
    from flask import request
    data = request.get_json(silent=True) or {}
    uid = data.get("uid")
    chat_id = data.get("chat_id")
    job_id = data.get("id", "").strip()
    schedule = data.get("schedule", "").strip()
    prompt = data.get("prompt", "").strip()
    if not all([uid, chat_id, job_id, schedule, prompt]):
        return {"error": "Missing required fields: uid, chat_id, id, schedule, prompt"}, 400
    try:
        uid, chat_id = int(uid), int(chat_id)
    except (ValueError, TypeError):
        return {"error": "Invalid uid or chat_id"}, 400
    # Validate cron expression
    try:
        from croniter import croniter
        cron = croniter(schedule, datetime.now())
        next_run = cron.get_next(datetime).isoformat()
    except Exception as e:
        return {"error": f"Invalid cron schedule: {e}"}, 400
    jobs = _load_cron_jobs(uid, chat_id)
    # Remove example job if present
    jobs = [j for j in jobs if j.get("id") != "example"]
    # Check for duplicate id
    if any(j.get("id") == job_id for j in jobs):
        return {"error": f"Job ID '{job_id}' already exists"}, 409
    jobs.append({"id": job_id, "schedule": schedule, "prompt": prompt, "next_run": next_run})
    _save_cron_jobs(uid, chat_id, jobs)
    tui_log(f"[magenta]⏰ cron added[/] {uid}:{chat_id} job={job_id} schedule={schedule}")
    return {"ok": True, "next_run": next_run}


@app.route("/cronjobs/<int:uid>/<int:chat_id>/<job_id>", methods=["DELETE"])
def _web_delete_cronjob(uid, chat_id, job_id):
    jobs = _load_cron_jobs(uid, chat_id)
    new_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        return {"error": "Job not found"}, 404
    _save_cron_jobs(uid, chat_id, new_jobs)
    tui_log(f"[magenta]⏰ cron deleted[/] {uid}:{chat_id} job={job_id}")
    return {"ok": True}


@app.route("/chats")
def _web_chats():
    """Return all known chats from agents.json for the cronjob chat picker."""
    chats = []
    if os.path.exists(SESSIONS_PATH):
        try:
            with open(SESSIONS_PATH) as f:
                data = json.load(f)
            for key_str, entry in data.items():
                uid_str, chat_str = key_str.split(":", 1)
                key = (int(uid_str), int(chat_str))
                label = chat_labels.get(key, key_str)
                cwd = ""
                if isinstance(entry, dict):
                    cwd = entry.get("cwd", "")
                chats.append({"uid": int(uid_str), "chat_id": int(chat_str), "label": label, "cwd": cwd})
        except Exception:
            pass
    return {"chats": chats}


# ── Web chat ──────────────────────────────────────────────────────────────────

_WEB_CHAT_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Chat</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #eff1f5; --bg2: #e6e9ef; --bg3: #dce0e8;
    --border: #bcc0cc; --fg: #4c4f69; --fg2: #6c6f85; --fg3: #8c8fa1;
    --accent: #1e66f5; --user-bg: #1e66f5; --user-fg: #fff;
    --bot-bg: #e6e9ef; --bot-fg: #4c4f69;
  }
  body.dark {
    --bg: #1e1e2e; --bg2: #181825; --bg3: #313244;
    --border: #313244; --fg: #cdd6f4; --fg2: #6c7086; --fg3: #45475a;
    --accent: #89b4fa; --user-bg: #89b4fa; --user-fg: #1e1e2e;
    --bot-bg: #313244; --bot-fg: #cdd6f4;
  }
  html, body { height: 100%; }
  body { background: var(--bg); color: var(--fg); font-family: system-ui, sans-serif;
         font-size: .85rem; display: flex; flex-direction: column; }
  #topbar { padding: .55rem 1rem; background: var(--bg2); border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: .6rem; font-weight: 600; flex-shrink: 0; }
  #topbar-name { flex: 1; }
  #theme-btn { background: none; border: 1px solid var(--border); border-radius: 6px;
               padding: .2rem .55rem; cursor: pointer; font-size: .85rem; color: var(--fg2); }
  #messages { flex: 1; overflow-y: auto; padding: 1rem; display: flex; flex-direction: column; gap: .6rem; }
  .msg { max-width: 75%; padding: .55rem .8rem; border-radius: 12px; line-height: 1.45;
         white-space: pre-wrap; word-break: break-word; }
  .msg.user { align-self: flex-end; background: var(--user-bg); color: var(--user-fg);
              border-bottom-right-radius: 3px; }
  .msg.assistant { align-self: flex-start; background: var(--bot-bg); color: var(--bot-fg);
                   border-bottom-left-radius: 3px; }
  .msg.cron { align-self: flex-start; background: var(--bg3); color: var(--fg2);
              border-bottom-left-radius: 3px; font-size: .78rem; }
  .msg-meta { font-size: .68rem; color: var(--fg3); margin-top: .2rem; }
  .msg.user .msg-meta { text-align: right; }
  #inputbar { padding: .65rem 1rem; background: var(--bg2); border-top: 1px solid var(--border);
              display: flex; gap: .5rem; flex-shrink: 0; }
  #inp { flex: 1; background: var(--bg); border: 1px solid var(--border); border-radius: 8px;
         padding: .45rem .75rem; color: var(--fg); font-size: .85rem; font-family: inherit;
         resize: none; min-height: 38px; max-height: 150px; outline: none; }
  #inp:focus { border-color: var(--accent); }
  #send-btn { background: var(--accent); color: #fff; border: none; border-radius: 8px;
              padding: .45rem .9rem; cursor: pointer; font-size: .85rem; font-weight: 600; }
  #send-btn:hover { opacity: .85; }
  #send-btn:disabled { opacity: .4; cursor: default; }
</style>
</head>
<body class="light">
<div id="topbar">
  <span id="topbar-name">Chat</span>
  <button id="theme-btn" onclick="toggleTheme()">&#9728;</button>
</div>
<div id="messages"></div>
<div id="inputbar">
  <textarea id="inp" rows="1" placeholder="Message…"></textarea>
  <button id="send-btn" onclick="sendMsg()">Send</button>
</div>
<script>
  const UID = %UID%;
  const CHAT_ID = %CHAT_ID%;
  const LABEL = %LABEL%;

  document.title = LABEL;
  document.getElementById('topbar-name').textContent = LABEL;

  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  function fmtTime(ts) {
    return new Date(ts * 1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
  }

  function appendMsg(role, text, ts, scroll=true) {
    const el = document.createElement('div');
    el.className = 'msg ' + role;
    el.innerHTML = esc(text) + `<div class="msg-meta">${fmtTime(ts)}</div>`;
    document.getElementById('messages').appendChild(el);
    if (scroll) el.scrollIntoView({behavior: 'smooth'});
  }

  // Load history then subscribe to SSE
  async function init() {
    try {
      const r = await fetch(`/chat/${UID}/${CHAT_ID}/history`);
      const d = await r.json();
      d.messages.forEach(m => appendMsg(m.role, m.text, m.ts, false));
      const msgs = document.getElementById('messages');
      msgs.scrollTop = msgs.scrollHeight;
    } catch {}

    const es = new EventSource(`/chat/${UID}/${CHAT_ID}/stream`);
    es.onmessage = e => {
      const d = JSON.parse(e.data);
      if (d.ping) return;
      appendMsg(d.role, d.text, d.ts);
    };
  }
  init();

  async function sendMsg() {
    const inp = document.getElementById('inp');
    const text = inp.value.trim();
    if (!text) return;
    inp.value = '';
    inp.style.height = '';
    document.getElementById('send-btn').disabled = true;
    try {
      await fetch(`/chat/${UID}/${CHAT_ID}/send`, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({text}),
      });
    } catch {}
    document.getElementById('send-btn').disabled = false;
    inp.focus();
  }

  document.getElementById('inp').addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendMsg(); }
  });

  // Auto-resize textarea
  document.getElementById('inp').addEventListener('input', function() {
    this.style.height = 'auto';
    this.style.height = Math.min(this.scrollHeight, 150) + 'px';
  });

  function toggleTheme() {
    const dark = document.body.classList.toggle('dark');
    document.body.classList.toggle('light', !dark);
    localStorage.setItem('chat-theme', dark ? 'dark' : 'light');
    document.getElementById('theme-btn').innerHTML = dark ? '&#9728;' : '&#9790;';
  }
  if (localStorage.getItem('chat-theme') === 'dark') {
    document.body.classList.add('dark');
    document.body.classList.remove('light');
    document.getElementById('theme-btn').innerHTML = '&#9728;';
  }
</script>
</body>
</html>"""


@app.route("/chat/<int:uid>/<int:chat_id>")
def _web_chat_page(uid, chat_id):
    key = (uid, chat_id)
    label = chat_labels.get(key, f"{uid}:{chat_id}")
    html = (_WEB_CHAT_HTML
            .replace("%UID%", str(uid))
            .replace("%CHAT_ID%", str(chat_id))
            .replace("%LABEL%", json.dumps(label)))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/chat/<int:uid>/<int:chat_id>/history")
def _web_chat_history(uid, chat_id):
    return {"messages": chat_history.get((uid, chat_id), [])}


@app.route("/chat/<int:uid>/<int:chat_id>/stream")
def _web_chat_stream(uid, chat_id):
    key = (uid, chat_id)
    q = queue.Queue(maxsize=200)
    with _chat_sse_lock:
        _chat_sse.setdefault(key, []).append(q)

    @stream_with_context
    def generate():
        try:
            while True:
                try:
                    event = q.get(timeout=25)
                    yield f"data: {json.dumps(event)}\n\n"
                except queue.Empty:
                    yield 'data: {"ping":true}\n\n'
        finally:
            with _chat_sse_lock:
                subs = _chat_sse.get(key, [])
                if q in subs:
                    subs.remove(q)

    return generate(), {"Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.route("/chat/<int:uid>/<int:chat_id>/send", methods=["POST"])
def _web_chat_send(uid, chat_id):
    from flask import request
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return {"error": "empty"}, 400
    key = (uid, chat_id)
    if key not in chat_labels:
        return {"error": "unknown chat"}, 404
    # Echo to Telegram so the conversation is visible there too
    try:
        bot.send_message(chat_id, f"🌐 {text}")
    except Exception:
        pass
    _chat_push(uid, chat_id, "user", text)
    msg = _WebMessage(uid, chat_id, text)
    _get_user_queue(uid, chat_id).put(msg)
    return {"ok": True}


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
    global _current_proc
    last_error = ""
    for attempt in range(max_retries + 1):
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd=cwd)
            _current_proc = proc
            try:
                stdout, stderr = proc.communicate(timeout=timeout or TIMEOUT)
            finally:
                _current_proc = None
            if proc.returncode != 0 or not stdout.strip():
                last_error = stderr.strip()
            else:
                data = json.loads(stdout)
                text = (data.get("result") or "").strip() or "(empty response)"
                return text, data.get("session_id", "")
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.communicate()
            _current_proc = None
            return "(Claude timed out)", ""
        except FileNotFoundError:
            _current_proc = None
            return "(claude CLI not found — install @anthropic-ai/claude-code)", ""
        if attempt < max_retries:
            tui_log(f"[yellow]⚠ empty response, retrying ({attempt + 1}/{max_retries})…[/]")
            time.sleep(retry_delay)
    return (f"[Error] {last_error}" if last_error else "(empty response)"), ""


# ── Web chat history & SSE ────────────────────────────────────────────────────

_CHAT_HISTORY_MAX = 200

def _chat_push(uid: int, chat_id: int, role: str, text: str):
    """Append a message to per-chat history and broadcast to web chat SSE subscribers."""
    key = (uid, chat_id)
    entry = {"role": role, "text": text, "ts": time.time()}
    history = chat_history.setdefault(key, [])
    history.append(entry)
    if len(history) > _CHAT_HISTORY_MAX:
        del history[:-_CHAT_HISTORY_MAX]
    with _chat_sse_lock:
        subs = list(_chat_sse.get(key, []))
    dead = []
    for q in subs:
        try:
            q.put_nowait(entry)
        except queue.Full:
            dead.append(q)
    if dead:
        with _chat_sse_lock:
            _chat_sse[key] = [q for q in _chat_sse.get(key, []) if q not in dead]


class _WebMessage:
    """Fake Telegram message object for messages originating from the web UI."""
    _from_web = True

    def __init__(self, uid: int, chat_id: int, text: str, username: str = "web"):
        import types
        self.from_user = types.SimpleNamespace(id=uid, username=username)
        self.chat = types.SimpleNamespace(id=chat_id, type="private", title=None)
        self.text = text
        self.caption = None
        self.document = None
        self.message_id = 0


# ── Message processing ────────────────────────────────────────────────────────

def _send_markdown(message, text: str):
    """Send text with MarkdownV2 formatting, falling back to plain text."""
    if isinstance(message, _WebMessage):
        # Web-originated message: send back to Telegram via send_message (no reply_to)
        try:
            bot.send_message(message.chat.id, telegramify_markdown.markdownify(text), parse_mode="MarkdownV2")
        except Exception:
            bot.send_message(message.chat.id, text)
    else:
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
    """Send typing action immediately, then every 3s until done.

    Telegram's typing indicator expires after ~5s; 3s gives enough headroom
    to survive occasional network latency without the indicator dropping."""
    while True:
        try:
            bot.send_chat_action(chat_id, "typing")
        except Exception:
            pass
        if done.wait(timeout=3):
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
    _ensure_cron_file(uid, chat_id)
    custom_preamble = _read_preamble(uid, chat_id)
    memory_prefix = (
        f"[Agent chat:{uid}:{chat_id} | memory:{memory_path} | crons:{crons_path} | preamble:{_preamble_path(uid, chat_id)}]\n"
        f"Always reply after tool use.\n"
        f"Do NOT use built-in CronCreate, CronList, CronDelete tools. Manage crons by reading/writing the crons JSON file at {crons_path} directly.\n\n"
        + (f"{GLOBAL_PREAMBLE}\n\n" if GLOBAL_PREAMBLE else "")
        + (f"{custom_preamble}\n\n" if custom_preamble else "")
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

    if key in _cancelled_keys:
        _cancelled_keys.discard(key)
        return
    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    last_reply[key] = reply[:300]
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[blue]←[/] [bold]{escape(username)}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    _chat_push(uid, chat_id, "assistant", reply)
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
    _ensure_cron_file(uid, chat_id)
    custom_preamble = _read_preamble(uid, chat_id)
    preamble = (
        f"[Agent chat:{uid}:{chat_id} | memory:{memory_path} | crons:{crons_path} | preamble:{_preamble_path(uid, chat_id)}]\n"
        f"Always reply after tool use.\n"
        f"Do NOT use built-in CronCreate, CronList, CronDelete tools. Manage crons by reading/writing the crons JSON file at {crons_path} directly.\n\n"
        + (f"{GLOBAL_PREAMBLE}\n\n" if GLOBAL_PREAMBLE else "")
        + (f"{custom_preamble}\n\n" if custom_preamble else "")
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

    if key in _cancelled_keys:
        _cancelled_keys.discard(key)
        return
    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    last_reply[key] = reply[:300]
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[magenta]←[/] [bold]cron:{job_id}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    _chat_push(uid, chat_id, "assistant", reply)
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
        "/cancel — stop current task and clear pending messages\n"
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


def cmd_cancel(message):
    if not _allowed(message): return
    uid = message.from_user.id
    key = (uid, message.chat.id)
    parts = []
    if claude_busy_key == key and _current_proc is not None:
        _current_proc.kill()
        _cancelled_keys.add(key)
        parts.append("current task stopped")
    q = user_queues.get(key)
    drained = 0
    if q:
        while True:
            try:
                q.get_nowait()
                drained += 1
            except queue.Empty:
                break
    if drained:
        parts.append(f"{drained} pending message(s) cleared")
    if parts:
        bot.reply_to(message, "🛑 " + ", ".join(parts).capitalize() + ".")
    else:
        bot.reply_to(message, "Nothing to cancel.")


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
    _chat_push(uid, message.chat.id, "user", message.text or "")
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
    telebot_log.addHandler(_LogHandler())
    telebot_log.propagate = False
    bot = telebot.TeleBot(BOT_TOKEN, num_threads=8)
    bot.message_handler(commands=["start"])(cmd_start)
    bot.message_handler(commands=["help"])(cmd_help)
    bot.message_handler(commands=["clear"])(cmd_clear)
    bot.message_handler(commands=["cancel"])(cmd_cancel)
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
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, debug=False, use_reloader=False)
