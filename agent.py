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


def init(config: dict):
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, MODEL, WEB_PORT
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    MODEL = config.get("model", "claude-sonnet-4-6")
    WEB_PORT = config.get("web_port", 17488)
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
    .cron-prompt { color: var(--preview); max-width: 320px;
                   white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
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
</div>

<script>
  function esc(s) {
    return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');
  }

  // ── Nav switching ──
  let currentView = 'chats';
  const VIEW_LABELS = { chats: 'Chats', crons: 'Cronjobs', log: 'Log', status: 'Status' };

  function selectView(name) {
    currentView = name;
    document.querySelectorAll('.nav-item').forEach(el => {
      el.classList.toggle('sel', el.dataset.view === name);
    });
    document.querySelectorAll('.view').forEach(el => {
      el.classList.toggle('sel', el.id === 'view-' + name);
    });
    document.getElementById('topbar-label').firstElementChild.textContent = VIEW_LABELS[name] || name;
    document.getElementById('topbar-sub').textContent = '';
  }

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
            <div>
              <button class="cc-save" onclick="savePreamble(this)">Save</button>
              <span class="cc-saved">Saved ✓</span>
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
                <td class="cron-prompt" title="${esc(j.prompt)}">${esc(j.prompt)}</td>
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

    } catch {}
  }
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
        f"Always reply after tool use.\n\n"
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

    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    last_reply[key] = reply[:300]
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
    _ensure_cron_file(uid, chat_id)
    custom_preamble = _read_preamble(uid, chat_id)
    preamble = (
        f"[Agent chat:{uid}:{chat_id} | memory:{memory_path} | crons:{crons_path} | preamble:{_preamble_path(uid, chat_id)}]\n"
        f"Always reply after tool use.\n\n"
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

    if new_session_id:
        user_sessions[key] = new_session_id
        save_sessions()
    msg_counts[key] = msg_counts.get(key, 0) + 1
    last_reply[key] = reply[:300]
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
    telebot_log.addHandler(_LogHandler())
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
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, debug=False, use_reloader=False)
