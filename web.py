"""Web UI for CTA — Flask routes, SSE log/chat broadcast, and HTML templates."""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import threading
from collections import deque
from datetime import datetime

from flask import Flask, Response, request, stream_with_context
from werkzeug.routing import BaseConverter

# Set by agent.py via web.init() after its globals are fully initialised.
agent = None

def init(agent_module) -> None:
    global agent
    agent = agent_module

# ── Logging primitives ─────────────────────────────────────────────────────────

_log_entries: deque[tuple[str, str]] = deque(maxlen=200)
_tui_lock = threading.Lock()
_sse_subscribers: list[queue.Queue] = []
_sse_lock = threading.Lock()
_RICH_TAG = re.compile(r"\[/?[^\]]*\]")

app = Flask(__name__)
app.logger.disabled = True
logging.getLogger("werkzeug").setLevel(logging.ERROR)

# Flask's built-in <int:> only matches non-negative integers; Telegram group chat_ids are negative
from werkzeug.routing import BaseConverter
class _SignedInt(BaseConverter):
    regex = r"-?\d+"
    def to_python(self, value): return int(value)
    def to_url(self, value): return str(value)
app.url_map.converters["sint"] = _SignedInt


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

# ── Web message (synthetic Telegram message for web-originated sends) ─────────

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
        self.voice = None
        self.audio = None
        self.message_id = 0


# ── Main UI HTML template ─────────────────────────────────────────────────────

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
    .cc-val.stale { color: #e64553; }
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
    .cc-delete { margin-top: .35rem; margin-left: auto; padding: .3rem .7rem;
                 background: none; border: 1px solid #e64553; border-radius: 4px;
                 color: #e64553; font-size: .72rem; cursor: pointer; }
    .cc-delete:hover { background: #e64553; color: #fff; }
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
    .cron-del, .cron-edit { background: none; border: 1px solid var(--border); border-radius: 4px;
                color: var(--fg2); font-size: .7rem; padding: .15rem .45rem; cursor: pointer; margin-right: .2rem; }
    .cron-del:hover { background: #e64553; color: #fff; border-color: #e64553; }
    .cron-edit:hover { background: var(--accent); color: #fff; border-color: var(--accent); }
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
        <h3 id="cron-form-title">Add Cronjob</h3>
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
          <button class="cron-form-btn" id="cron-submit-btn" onclick="saveCron()">Add</button>
          <button class="cron-form-btn" id="cron-cancel-btn" onclick="cancelEdit()" style="display:none;background:var(--bg3)">Cancel</button>
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
        <label>Max concurrent agents</label>
        <input id="cfg-concurrent" type="number" min="1" max="20" />
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
      <div class="cfg-row">
        <label>PATH prefix</label>
        <input id="cfg-path-prefix" type="text" placeholder="colon-separated dirs to prepend to PATH, e.g. ~/bin:/opt/homebrew/bin" />
      </div>
      <div class="cfg-row" style="flex-direction:column;align-items:flex-start;gap:0.4rem;">
        <label>System preamble <span style="font-weight:400;color:var(--fg3);font-size:.75rem;">(hardcoded; injected before Global preamble on every turn)</span></label>
        <pre id="cfg-system-preamble" style="width:100%;box-sizing:border-box;font-family:inherit;font-size:0.82rem;background:var(--bg3);color:var(--fg2);border:1px solid var(--border);border-radius:4px;padding:0.5rem;white-space:pre-wrap;word-break:break-word;margin:0;"></pre>
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

  function timeAgo(epochSec) {
    if (!epochSec) return '—';
    const sec = Math.max(0, Math.floor(Date.now()/1000 - epochSec));
    if (sec < 60) return `${sec}s ago`;
    if (sec < 3600) return `${Math.floor(sec/60)}m ago`;
    if (sec < 86400) return `${Math.floor(sec/3600)}h ago`;
    return `${Math.floor(sec/86400)}d ago`;
  }

  // ── Nav switching ──
  let currentView = 'chats';
  let chatUid = null, chatChatId = null, chatES = null;
  const VIEW_LABELS = { chats: 'Chats', crons: 'Cronjobs', log: 'Log', status: 'Status', config: 'Config' };

  function viewFromHash() {
    const h = location.hash.replace(/^#/, '');
    if (h.startsWith('chat/')) {
      const parts = h.split('/');
      if (parts.length === 3) return {view: 'chat', uid: +parts[1], chatId: +parts[2]};
    }
    return {view: VIEW_LABELS[h] ? h : 'chats'};
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
    if (e.state && e.state.view === 'chat') {
      openChat(e.state.uid, e.state.chatId, e.state.label || '…');
    } else {
      const nav = viewFromHash();
      if (nav.view === 'chat') openChat(nav.uid, nav.chatId, '…');
      else selectView(nav.view);
    }
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

  async function deleteChat(uid, chat_id, label) {
    if (!confirm(`Delete "${label}"?\n\nThis removes session, messages, memory, crons, and preamble for this chat. The Telegram chat itself is unaffected — sending another message there will start fresh.`)) return;
    const r = await fetch(`/chat/${uid}/${chat_id}`, {method: 'DELETE'});
    if (r.status === 409) {
      alert('Chat is busy with a running Claude turn. Try again after it finishes.');
      return;
    }
    if (!r.ok) {
      alert('Delete failed: HTTP ' + r.status);
      return;
    }
    tick();
  }

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
            <div class="cc-row"><span class="cc-key">active</span><span class="cc-val${s.last_active && (Date.now()/1000 - s.last_active) > 7*86400 ? ' stale' : ''}">${timeAgo(s.last_active)}</span></div>
            ${s.last_reply ? `<div class="cc-preview">${esc(s.last_reply)}</div>` : ''}
            <div class="cc-preamble-label">Custom preamble</div>
            <textarea class="cc-preamble" data-uid="${s.uid}" data-chat="${s.chat_id}">${esc(preambleVal)}</textarea>
            <div style="display:flex;gap:.4rem;align-items:center">
              <button class="cc-save" onclick="savePreamble(this)">Save preamble</button>
              <span class="cc-saved">Saved ✓</span>
              <button class="cc-open" onclick="openChat(${s.uid},${s.chat_id},'${esc(s.label).replace(/'/g,"\\'")}')">Open chat</button>
              <button class="cc-delete" onclick="deleteChat(${s.uid},${s.chat_id},'${esc(s.label).replace(/'/g,"\\'")}')">Delete</button>
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
                <td style="white-space:nowrap">
                  <button class="cron-edit" onclick="editCron(this)" data-uid="${j.uid}" data-chatid="${j.chat_id}" data-jobid="${esc(j.id)}" data-schedule="${esc(j.schedule)}" data-prompt="${esc(j.prompt)}">✎</button>
                  <button class="cron-del" onclick="delCron(${j.uid},${j.chat_id},'${esc(j.id)}')">✕</button>
                </td>
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
  const _initNav = viewFromHash();
  history.replaceState({view: _initNav.view, uid: _initNav.uid, chatId: _initNav.chatId}, '', location.href);
  _popNav = true;
  if (_initNav.view === 'chat') openChat(_initNav.uid, _initNav.chatId, '…');
  else selectView(_initNav.view);
  _popNav = false;
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

  let _editingCron = null; // {uid, chatId, jobId} when in edit mode

  function editCron(btn) {
    const uid = +btn.dataset.uid, chatId = +btn.dataset.chatid;
    const jobId = btn.dataset.jobid, schedule = btn.dataset.schedule, prompt = btn.dataset.prompt;
    _editingCron = {uid, chatId, jobId};
    document.getElementById('cron-form-title').textContent = 'Edit Cronjob';
    document.getElementById('cron-submit-btn').textContent = 'Save';
    document.getElementById('cron-cancel-btn').style.display = '';
    const sel = document.getElementById('cron-chat-sel');
    sel.value = `${uid}:${chatId}`;
    sel.disabled = true;
    const idInp = document.getElementById('cron-id-inp');
    idInp.value = jobId;
    idInp.readOnly = true;
    document.getElementById('cron-sched-inp').value = schedule;
    document.getElementById('cron-prompt-inp').value = prompt;
    document.getElementById('cron-form').scrollIntoView({behavior: 'smooth'});
  }

  function cancelEdit() {
    _editingCron = null;
    document.getElementById('cron-form-title').textContent = 'Add Cronjob';
    document.getElementById('cron-submit-btn').textContent = 'Add';
    document.getElementById('cron-cancel-btn').style.display = 'none';
    document.getElementById('cron-chat-sel').disabled = false;
    document.getElementById('cron-id-inp').readOnly = false;
    document.getElementById('cron-id-inp').value = '';
    document.getElementById('cron-sched-inp').value = '';
    document.getElementById('cron-prompt-inp').value = '';
  }

  async function saveCron() {
    const msg = document.getElementById('cron-form-msg');
    const schedule = document.getElementById('cron-sched-inp').value.trim();
    const prompt = document.getElementById('cron-prompt-inp').value.trim();
    if (!schedule) { showMsg(msg, 'Enter a cron schedule', true); return; }
    if (!prompt) { showMsg(msg, 'Enter a prompt', true); return; }

    if (_editingCron) {
      // Update existing
      const {uid, chatId, jobId} = _editingCron;
      try {
        const r = await fetch(`/cronjobs/${uid}/${chatId}/${jobId}`, {
          method: 'PUT', headers: {'Content-Type': 'application/json'},
          body: JSON.stringify({schedule, prompt}),
        });
        const d = await r.json();
        if (!r.ok) { showMsg(msg, d.error || 'Failed', true); return; }
        showMsg(msg, 'Saved! Next run: ' + d.next_run.replace('T',' ').slice(0,16), false);
        cancelEdit();
        tick();
      } catch { showMsg(msg, 'Network error', true); }
    } else {
      // Add new
      const sel = document.getElementById('cron-chat-sel').value;
      const id = document.getElementById('cron-id-inp').value.trim();
      if (!sel) { showMsg(msg, 'Select a chat', true); return; }
      if (!id) { showMsg(msg, 'Enter a job ID', true); return; }
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
      } catch { showMsg(msg, 'Network error', true); }
    }
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
      document.getElementById('cfg-concurrent').value = d.max_concurrent_claude ?? 1;
      document.getElementById('cfg-port').value = d.web_port ?? '';
      document.getElementById('cfg-cwd').value = d.default_cwd || '';
      document.getElementById('cfg-users').value = (d.allowed_users || []).join(', ');
      document.getElementById('cfg-path-prefix').value = d.path_prefix || '';
      document.getElementById('cfg-system-preamble').textContent = d.system_preamble || '';
      document.getElementById('cfg-global-preamble').value = d.global_preamble || '';
    } catch {}
  }

  async function saveConfig() {
    const msg = document.getElementById('cfg-msg');
    const token = document.getElementById('cfg-token').value.trim();
    const body = {
      model: document.getElementById('cfg-model').value,
      claude_timeout: parseInt(document.getElementById('cfg-timeout').value) || 1800,
      max_concurrent_claude: parseInt(document.getElementById('cfg-concurrent').value) || 1,
      web_port: parseInt(document.getElementById('cfg-port').value) || 17488,
      default_cwd: document.getElementById('cfg-cwd').value.trim(),
      allowed_users: document.getElementById('cfg-users').value
        .split(',').map(s => s.trim()).filter(Boolean).map(Number),
      path_prefix: document.getElementById('cfg-path-prefix').value.trim(),
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

    // Update URL
    if (!_popNav) {
      history.pushState({view: 'chat', uid, chatId, label}, '', `#chat/${uid}/${chatId}`);
    }

    // Update topbar and switch view
    document.getElementById('topbar-label').firstElementChild.textContent = label;
    document.getElementById('topbar-sub').textContent = '';
    // Deselect nav items (chat isn't in the nav)
    document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('sel'));
    document.querySelectorAll('.view').forEach(el => el.classList.remove('sel'));
    document.getElementById('view-chat').classList.add('sel');
    currentView = 'chat';

    // Resolve real label from server (needed when opened via direct URL)
    fetch('/status').then(r => r.json()).then(d => {
      const s = d.sessions.find(s => s.uid === uid && s.chat_id === chatId);
      if (s && s.label) {
        document.getElementById('topbar-label').firstElementChild.textContent = s.label;
        history.replaceState({view: 'chat', uid, chatId, label: s.label}, '', location.hash);
      }
    }).catch(() => {});

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


# ── Main UI routes ────────────────────────────────────────────────────────────

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
    all_keys = (set(agent.user_sessions) | set(agent.msg_counts)
                | set(agent.chat_labels) | set(agent.last_active))
    sessions = []
    for key in sorted(all_keys):
        uid, chat_id = key
        sessions.append({
            "label": agent.chat_labels.get(key, f"{uid}:{chat_id}"),
            "uid": uid,
            "chat_id": chat_id,
            "model": agent.user_model.get(key, agent.MODEL),
            "cwd": agent.user_cwd.get(key, agent.DEFAULT_CWD).replace(os.path.expanduser("~"), "~", 1),
            "msgs": agent.msg_counts.get(key, 0),
            "active": key in agent.claude_active_keys,
            "last_active": agent.last_active.get(key, 0),
            "last_reply": agent.last_reply.get(key, ""),
            "preamble": agent._read_preamble(uid, chat_id),
        })
    return {"model": agent.MODEL, "cwd": agent.DEFAULT_CWD, "sessions": sessions}


@app.route("/config", methods=["GET"])
def _web_get_config():
    try:
        with open(agent.CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    return {
        "telegram_bot_token": cfg.get("telegram_bot_token", agent.BOT_TOKEN),
        "model": cfg.get("model", agent.MODEL),
        "claude_timeout": cfg.get("claude_timeout", agent.TIMEOUT),
        "web_port": cfg.get("web_port", agent.WEB_PORT),
        "default_cwd": cfg.get("default_cwd", agent.DEFAULT_CWD),
        "allowed_users": cfg.get("allowed_users", list(agent.ALLOWED_USERS)),
        "max_concurrent_claude": cfg.get("max_concurrent_claude", agent.MAX_CONCURRENT_CLAUDE),
        "path_prefix": cfg.get("path_prefix", agent.PATH_PREFIX),
        "global_preamble": agent._read_global_preamble(),
        "system_preamble": agent._system_preamble("<uid>", "<chat_id>"),
    }


@app.route("/config", methods=["POST"])
def _web_set_config():
    data = request.get_json(silent=True) or {}
    try:
        with open(agent.CONFIG_PATH) as f:
            cfg = json.load(f)
    except Exception:
        cfg = {}
    if data.get("telegram_bot_token"):
        cfg["telegram_bot_token"] = data["telegram_bot_token"]
        agent.BOT_TOKEN = data["telegram_bot_token"]
    if "model" in data and data["model"]:
        cfg["model"] = data["model"]
        agent.MODEL = data["model"]
    if "claude_timeout" in data and data["claude_timeout"] > 0:
        cfg["claude_timeout"] = data["claude_timeout"]
        agent.TIMEOUT = data["claude_timeout"]
    if "web_port" in data and data["web_port"]:
        cfg["web_port"] = data["web_port"]
    if "default_cwd" in data and data["default_cwd"]:
        expanded = os.path.expanduser(data["default_cwd"])
        cfg["default_cwd"] = expanded
        agent.DEFAULT_CWD = expanded
    if "allowed_users" in data:
        cfg["allowed_users"] = [int(u) for u in data["allowed_users"] if str(u).strip()]
        agent.ALLOWED_USERS = set(cfg["allowed_users"])
    if "max_concurrent_claude" in data:
        try:
            n = max(1, int(data["max_concurrent_claude"]))
        except (TypeError, ValueError):
            n = agent.MAX_CONCURRENT_CLAUDE
        if n != agent.MAX_CONCURRENT_CLAUDE:
            agent.MAX_CONCURRENT_CLAUDE = n
            # Allocating a fresh semaphore is OK because in-flight calls
            # capture a local reference at acquire time and release that
            # same instance. Old waiters will still drain on the previous
            # semaphore once their permits land.
            agent._claude_semaphore = threading.Semaphore(n)
        cfg["max_concurrent_claude"] = n
    if "path_prefix" in data:
        prefix = data["path_prefix"].strip()
        cfg["path_prefix"] = prefix
        agent.PATH_PREFIX = prefix
        agent._apply_path_prefix(prefix)
    if "global_preamble" in data:
        text = data["global_preamble"].strip()
        if text:
            with open(agent.GLOBAL_PREAMBLE_PATH, "w") as f:
                f.write(text)
        else:
            try:
                os.unlink(agent.GLOBAL_PREAMBLE_PATH)
            except FileNotFoundError:
                pass
        agent.GLOBAL_PREAMBLE = text
    try:
        tmp = agent.CONFIG_PATH + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.replace(tmp, agent.CONFIG_PATH)
    except Exception as e:
        return {"error": str(e)}, 500
    tui_log(f"[cyan]⚙ config updated via web[/]")
    return {"ok": True}


@app.route("/preamble/<sint:uid>/<sint:chat_id>", methods=["GET"])
def _web_get_preamble(uid, chat_id):
    return {"preamble": agent._read_preamble(uid, chat_id)}


@app.route("/preamble/<sint:uid>/<sint:chat_id>", methods=["POST"])
def _web_set_preamble(uid, chat_id):
    text = (request.get_json(silent=True) or {}).get("preamble", "").strip()
    path = agent._preamble_path(uid, chat_id)
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
    if os.path.isdir(agent.CRONS_DIR):
        for fname in sorted(os.listdir(agent.CRONS_DIR)):
            if not fname.endswith(".json"):
                continue
            try:
                uid_str, chat_str = fname[:-5].split(":", 1)
                uid, chat_id = int(uid_str), int(chat_str)
            except ValueError:
                continue
            key = (uid, chat_id)
            label = agent.chat_labels.get(key, f"{uid}:{chat_id}")
            for job in agent._load_cron_jobs(uid, chat_id):
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
    jobs = agent._load_cron_jobs(uid, chat_id)
    # Remove example job if present
    jobs = [j for j in jobs if j.get("id") != "example"]
    # Check for duplicate id
    if any(j.get("id") == job_id for j in jobs):
        return {"error": f"Job ID '{job_id}' already exists"}, 409
    jobs.append({"id": job_id, "schedule": schedule, "prompt": prompt, "next_run": next_run})
    agent._save_cron_jobs(uid, chat_id, jobs)
    tui_log(f"[magenta]⏰ cron added[/] {uid}:{chat_id} job={job_id} schedule={schedule}")
    return {"ok": True, "next_run": next_run}


@app.route("/cronjobs/<sint:uid>/<sint:chat_id>/<job_id>", methods=["DELETE"])
def _web_delete_cronjob(uid, chat_id, job_id):
    jobs = agent._load_cron_jobs(uid, chat_id)
    new_jobs = [j for j in jobs if j.get("id") != job_id]
    if len(new_jobs) == len(jobs):
        return {"error": "Job not found"}, 404
    agent._save_cron_jobs(uid, chat_id, new_jobs)
    tui_log(f"[magenta]⏰ cron deleted[/] {uid}:{chat_id} job={job_id}")
    return {"ok": True}


@app.route("/cronjobs/<sint:uid>/<sint:chat_id>/<job_id>", methods=["PUT"])
def _web_update_cronjob(uid, chat_id, job_id):
    data = request.get_json(silent=True) or {}
    schedule = data.get("schedule", "").strip()
    prompt = data.get("prompt", "").strip()
    if not schedule or not prompt:
        return {"error": "Missing required fields: schedule, prompt"}, 400
    try:
        from croniter import croniter
        cron = croniter(schedule, datetime.now())
        next_run = cron.get_next(datetime).isoformat()
    except Exception as e:
        return {"error": f"Invalid cron schedule: {e}"}, 400
    jobs = agent._load_cron_jobs(uid, chat_id)
    for j in jobs:
        if j.get("id") == job_id:
            j["schedule"] = schedule
            j["prompt"] = prompt
            j["next_run"] = next_run
            agent._save_cron_jobs(uid, chat_id, jobs)
            tui_log(f"[magenta]⏰ cron updated[/] {uid}:{chat_id} job={job_id}")
            return {"ok": True, "next_run": next_run}
    return {"error": "Job not found"}, 404


@app.route("/chats")
def _web_chats():
    """Return all known chats from agents.json for the cronjob chat picker."""
    chats = []
    if os.path.exists(agent.AGENTS_PATH):
        try:
            with open(agent.AGENTS_PATH) as f:
                data = json.load(f)
            for key_str, entry in data.items():
                uid_str, chat_str = key_str.split(":", 1)
                key = (int(uid_str), int(chat_str))
                label = agent.chat_labels.get(key, key_str)
                cwd = ""
                if isinstance(entry, dict):
                    cwd = entry.get("cwd", "")
                chats.append({"uid": int(uid_str), "chat_id": int(chat_str), "label": label, "cwd": cwd})
        except Exception:
            pass
    return {"chats": chats}


# ── Per-chat UI HTML template + routes ────────────────────────────────────────

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
    --agent.bot-bg: #e6e9ef; --agent.bot-fg: #4c4f69;
  }
  body.dark {
    --bg: #1e1e2e; --bg2: #181825; --bg3: #313244;
    --border: #313244; --fg: #cdd6f4; --fg2: #6c7086; --fg3: #45475a;
    --accent: #89b4fa; --user-bg: #89b4fa; --user-fg: #1e1e2e;
    --agent.bot-bg: #313244; --agent.bot-fg: #cdd6f4;
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
  .msg.assistant { align-self: flex-start; background: var(--agent.bot-bg); color: var(--agent.bot-fg);
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


@app.route("/chat/<sint:uid>/<sint:chat_id>")
def _web_chat_page(uid, chat_id):
    key = (uid, chat_id)
    label = agent.chat_labels.get(key, f"{uid}:{chat_id}")
    html = (_WEB_CHAT_HTML
            .replace("%UID%", str(uid))
            .replace("%CHAT_ID%", str(chat_id))
            .replace("%LABEL%", json.dumps(label)))
    return html, 200, {"Content-Type": "text/html; charset=utf-8"}


@app.route("/chat/<sint:uid>/<sint:chat_id>/history")
def _web_chat_history(uid, chat_id):
    return {"messages": agent.chat_history.get((uid, chat_id), [])}


@app.route("/chat/<sint:uid>/<sint:chat_id>/stream")
def _web_chat_stream(uid, chat_id):
    key = (uid, chat_id)
    q = queue.Queue(maxsize=200)
    with agent._chat_sse_lock:
        agent._chat_sse.setdefault(key, []).append(q)

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
            with agent._chat_sse_lock:
                subs = agent._chat_sse.get(key, [])
                if q in subs:
                    subs.remove(q)

    return generate(), {"Content-Type": "text/event-stream",
                        "Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


@app.route("/chat/<sint:uid>/<sint:chat_id>/send", methods=["POST"])
def _web_chat_send(uid, chat_id):
    data = request.get_json(silent=True) or {}
    text = data.get("text", "").strip()
    if not text:
        return {"error": "empty"}, 400
    key = (uid, chat_id)
    if key not in agent.chat_labels:
        return {"error": "unknown chat"}, 404
    # Echo to Telegram so the conversation is visible there too
    try:
        agent.bot.send_message(chat_id, f"🌐 {text}")
    except Exception:
        pass
    agent._chat_push(uid, chat_id, "user", text)
    msg = _WebMessage(uid, chat_id, text)
    agent._get_user_queue(uid, chat_id).put(msg)
    return {"ok": True}


@app.route("/chat/<sint:uid>/<sint:chat_id>", methods=["DELETE"])
def _web_chat_delete(uid, chat_id):
    """Hard-delete all state for a chat (sessions, in-memory dicts, and the
    memory/crons/preamble files on disk). Refuses if Claude is currently
    running for this chat to avoid corrupting an in-flight turn."""
    key = (uid, chat_id)
    if key in agent.claude_active_keys:
        return {"error": "chat is busy — try again after the current turn"}, 409
    agent._purge_chat(uid, chat_id)
    return {"ok": True}


