#!/usr/bin/env python3
"""
CTA — Claude Telegram Agent.
Telegram bot powered by Claude Code CLI.
Uses Max subscription — no API tokens needed.
"""

from __future__ import annotations

import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections import deque
from datetime import datetime

import telebot
import telegramify_markdown
from rich.markup import escape

import claude_code
import backends
import web
from web import app, tui_log, _LogHandler, _WebMessage

# Ensure /usr/local/bin and ~/bin are in PATH so shell commands issued by Claude
# work even when agent.py is launched via launchd/systemd with a minimal environment.
# If PATH is unset, fall back to standard system dirs (not "" — which resolves to CWD).
_existing_path = os.environ.get("PATH", "")
_path_parts = _existing_path.split(os.pathsep)
_home_bin = os.path.expanduser("~/bin")
_prepend = [d for d in ["/usr/local/bin", _home_bin] if d not in _path_parts]
if _prepend:
    _base = _existing_path or "/usr/bin:/bin:/usr/sbin:/sbin"
    os.environ["PATH"] = os.pathsep.join(_prepend) + os.pathsep + _base

# ── Config ────────────────────────────────────────────────────────────────────

CTA_HOME = os.path.expanduser("~/.cta")
CONFIG_PATH = os.path.join(CTA_HOME, "config.json")
CTA_ROOT = os.path.dirname(os.path.abspath(__file__))
CRON_CLI_PATH = os.path.join(CTA_ROOT, "cron.py")
NOTIFY_CLI_PATH = os.path.join(CTA_ROOT, "notify.py")

DEFAULT_CONFIG = {
    "telegram_bot_token": "",
    "allowed_users": [],
    "claude_timeout": 1800,
    "max_concurrent_claude": 1,
    "model": "claude-sonnet-4-6",
    "web_port": 17488,
    "path_prefix": "",
}


def _apply_path_prefix(prefix: str) -> None:
    """Prepend `prefix` (os.pathsep-separated) to PATH, expanding ~ and skipping dupes."""
    if not prefix:
        return
    existing = os.environ.get("PATH", "")
    existing_parts = existing.split(os.pathsep) if existing else []
    new_parts = []
    for p in prefix.split(os.pathsep):
        p = os.path.expanduser(p.strip())
        if p and p not in existing_parts and p not in new_parts:
            new_parts.append(p)
    if new_parts:
        base = existing or "/usr/bin:/bin:/usr/sbin:/sbin"
        os.environ["PATH"] = os.pathsep.join(new_parts) + os.pathsep + base


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
TIMEOUT = 1800
MAX_CONCURRENT_CLAUDE = 1
_claude_semaphore = threading.Semaphore(MAX_CONCURRENT_CLAUDE)
MODEL = "claude-sonnet-4-6"
WEB_PORT = 17488
DEFAULT_CWD = os.getcwd()
PATH_PREFIX = ""
CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
AGENTS_PATH = os.path.join(CTA_HOME, "agents.json")
MEMORY_DIR = os.path.join(CTA_HOME, "memory")
CRONS_DIR = os.path.join(CTA_HOME, "crons")
PREAMBLE_DIR = os.path.join(CTA_HOME, "preamble")
DEBUG_DIR = os.path.join(CTA_HOME, "debug")
GLOBAL_PREAMBLE_PATH = os.path.join(CTA_HOME, "global_preamble.md")
GLOBAL_PREAMBLE = ""
WHISPER_MODEL = "base"
_whisper_model_instance = None

bot = None  # initialized in create_bot()
user_sessions: dict[tuple[int, int], str] = {}  # (uid, chat_id) → Claude session ID
user_cwd: dict[tuple[int, int], str] = {}  # (uid, chat_id) → working directory
user_model: dict[tuple[int, int], str] = {}  # (uid, chat_id) → model override
user_timeout: dict[tuple[int, int], int] = {}  # (uid, chat_id) → timeout override (seconds)
user_backend_mode: dict[tuple[int, int], str] = {}  # (uid, chat_id) → "print" (default) / "stream" / "pty"
_backends: dict[tuple[int, int], "backends.ClaudeBackend"] = {}  # (uid, chat_id) → live backend
user_queues: dict[tuple[int, int], queue.Queue] = {}
user_queues_lock = threading.Lock()
chat_labels: dict[tuple[int, int], str] = {}   # (uid, chat_id) → "DM" or group name
msg_counts: dict[tuple[int, int], int] = {}    # (uid, chat_id) → messages processed
last_reply: dict[tuple[int, int], str] = {}    # (uid, chat_id) → last reply snippet
last_active: dict[tuple[int, int], float] = {} # (uid, chat_id) → epoch seconds of last user/assistant message
claude_active_keys: set = set()  # (uid, chat_id) keys with a running Claude call
_current_procs: dict = {}  # (uid, chat_id) → running Claude subprocess (killable)
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


def _system_preamble(uid_display, chat_id_display) -> str:
    """Build the hardcoded preamble prepended to every agent turn.

    Accepts uid/chat_id as stringifiable values so the /config endpoint can
    render a template with `<uid>` / `<chat_id>` placeholders for display.
    """
    memory_path = os.path.join(MEMORY_DIR, f"{uid_display}:{chat_id_display}.md")
    crons_path = os.path.join(CRONS_DIR, f"{uid_display}:{chat_id_display}.json")
    preamble_path = os.path.join(PREAMBLE_DIR, f"{uid_display}:{chat_id_display}.md")
    return (
        f"[Agent chat:{uid_display}:{chat_id_display} | memory:{memory_path} | crons:{crons_path} | preamble:{preamble_path}]\n"
        f"Always reply after tool use.\n"
        f"Do NOT use Telegram MCP plugin tools — agent.py handles replies.\n"
        f"Do NOT use built-in CronCreate, CronList, CronDelete tools. Manage crons with: python3 {CRON_CLI_PATH} add|list|remove|update (preferred — avoids JSON escape bugs; CTA_UID/CTA_CHAT_ID are already set in env). File at {crons_path} is fallback for inspection only.\n"
    )


def _load_whisper():
    """Lazily load the Whisper model (slow first call, cached after)."""
    global _whisper_model_instance
    if _whisper_model_instance is None:
        import whisper as _whisper
        tui_log(f"[cyan]🎙 loading Whisper model '{WHISPER_MODEL}'…[/]")
        _whisper_model_instance = _whisper.load_model(WHISPER_MODEL)
        tui_log(f"[cyan]🎙 Whisper ready[/]")
    return _whisper_model_instance


def init(config: dict):
    global BOT_TOKEN, ALLOWED_USERS, TIMEOUT, MODEL, WEB_PORT, DEFAULT_CWD, GLOBAL_PREAMBLE, WHISPER_MODEL
    global MAX_CONCURRENT_CLAUDE, _claude_semaphore, CLAUDE_BIN, PATH_PREFIX
    BOT_TOKEN = config["telegram_bot_token"]
    ALLOWED_USERS = set(config["allowed_users"])
    TIMEOUT = config["claude_timeout"]
    MAX_CONCURRENT_CLAUDE = max(1, int(config.get("max_concurrent_claude", 1)))
    _claude_semaphore = threading.Semaphore(MAX_CONCURRENT_CLAUDE)
    MODEL = config.get("model", "claude-sonnet-4-6")
    WEB_PORT = config.get("web_port", 17488)
    WHISPER_MODEL = config.get("whisper_model", "base")
    PATH_PREFIX = config.get("path_prefix", "")
    _apply_path_prefix(PATH_PREFIX)
    # Re-resolve CLAUDE_BIN AFTER applying path_prefix so an install discoverable
    # only via the new prefix is actually used by the print backend.
    CLAUDE_BIN = shutil.which("claude") or os.path.expanduser("~/.local/bin/claude")
    if config.get("default_cwd"):
        DEFAULT_CWD = os.path.expanduser(config["default_cwd"])
    GLOBAL_PREAMBLE = _read_global_preamble()
    os.makedirs(MEMORY_DIR, exist_ok=True)
    os.makedirs(CRONS_DIR, exist_ok=True)
    os.makedirs(PREAMBLE_DIR, exist_ok=True)
    os.makedirs(DEBUG_DIR, exist_ok=True)


def load_sessions():
    if not os.path.exists(AGENTS_PATH):
        return
    try:
        with open(AGENTS_PATH) as f:
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
                if entry.get("last_active"):
                    last_active[key] = entry["last_active"]
                if entry.get("label"):
                    chat_labels[key] = entry["label"]
                if entry.get("backend_mode") in ("stream", "pty"):
                    user_backend_mode[key] = entry["backend_mode"]
                elif entry.get("pty_mode"):  # legacy
                    user_backend_mode[key] = "pty"
        tui_log(f"[dim]Loaded {len(data)} agent(s) from {AGENTS_PATH}[/]")
    except Exception as e:
        tui_log(f"[red]Warning: could not load sessions: {escape(str(e))}[/]")


def save_sessions():
    tmp = AGENTS_PATH + ".tmp"
    try:
        all_keys = (set(user_sessions) | set(user_cwd) | set(user_model)
                    | set(last_active) | set(chat_labels) | set(user_backend_mode))
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
            if key in last_active:
                entry["last_active"] = last_active[key]
            if key in chat_labels:
                entry["label"] = chat_labels[key]
            mode = user_backend_mode.get(key, "print")
            if mode != "print":
                entry["backend_mode"] = mode
            data[f"{uid}:{chat_id}"] = entry
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, AGENTS_PATH)
    except Exception as e:
        tui_log(f"[red]Warning: could not save sessions: {escape(str(e))}[/]")


def _purge_chat(uid: int, chat_id: int):
    """Hard-delete all state for a chat: in-memory dicts + on-disk files
    (memory, crons, preamble). The chat disappears from the UI; if the user
    sends another message in that Telegram chat later, a fresh session is
    created from scratch."""
    key = (uid, chat_id)
    _stop_backend(key)
    for d in (user_sessions, user_cwd, user_model, user_timeout, user_backend_mode,
              msg_counts, last_reply, last_active, chat_labels, chat_history):
        d.pop(key, None)
    with _chat_sse_lock:
        _chat_sse.pop(key, None)
    with user_queues_lock:
        user_queues.pop(key, None)
    for path in (
        os.path.join(MEMORY_DIR, f"{uid}:{chat_id}.md"),
        os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json"),
        os.path.join(PREAMBLE_DIR, f"{uid}:{chat_id}.md"),
    ):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as e:
            tui_log(f"[red]Warning: could not delete {path}: {escape(str(e))}[/]")
    save_sessions()
    tui_log(f"[red]🗑 chat removed: {uid}:{chat_id}[/]")


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


def _crons_parse_error(uid: int, chat_id: int):
    """Return a short error message if the crons file exists but won't parse, else None.

    Silent parse failures in _load_cron_jobs used to hide broken files from both
    the agent and the UI. This helper lets callers surface the error (e.g. into
    the next preamble) so the agent that wrote the bad JSON can self-correct.
    """
    path = _cron_path(uid, chat_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path) as f:
            json.load(f)
        return None
    except json.JSONDecodeError as e:
        return f"line {e.lineno} col {e.colno}: {e.msg}"
    except Exception as e:
        return str(e)


def _save_cron_jobs(uid: int, chat_id: int, jobs: list):
    path = _cron_path(uid, chat_id)
    tmp = path + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(jobs, f, indent=2)
        os.replace(tmp, path)
    except Exception as e:
        tui_log(f"[red]Warning: could not save crons for {uid}:{chat_id}: {escape(str(e))}[/]")


def _cron_tick_once(now: datetime):
    """Run one scheduler tick: fire due jobs and persist next_run updates."""
    from croniter import croniter
    if not os.path.isdir(CRONS_DIR):
        return
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
                raw = job.get("next_run")
                try:
                    next_run = datetime.fromisoformat(raw) if raw else None
                except (TypeError, ValueError):
                    next_run = None
                if next_run is None:
                    # next_run missing or unparseable — default to next scheduled
                    # occurrence so jobs written directly to the JSON file
                    # (without the web UI) still run.
                    job["next_run"] = croniter(job["schedule"], now).get_next(datetime).isoformat()
                    changed = True
                    tui_log(f"[yellow]⏰ cron[/] {uid}:{chat_id} job={job['id']} next_run defaulted to {job['next_run']}")
                    continue
                if next_run <= now:
                    _get_user_queue(uid, chat_id).put({
                        "_type": "cron",
                        "uid": uid,
                        "chat_id": chat_id,
                        "job_id": job["id"],
                        "prompt": job["prompt"],
                    })
                    tui_log(f"[magenta]⏰ cron[/] {uid}:{chat_id} job={job['id']}")
                    job["next_run"] = croniter(job["schedule"], now).get_next(datetime).isoformat()
                    changed = True
            except Exception as e:
                tui_log(f"[red]cron error {uid}:{chat_id} job={job.get('id')}: {escape(str(e))}[/]")
        if changed:
            _save_cron_jobs(uid, chat_id, jobs)


def _cron_scheduler():
    """Background thread: fires due cron jobs into user queues every 60s."""
    while True:
        time.sleep(60)
        _cron_tick_once(datetime.now())


def _kill_tracked_subprocs() -> int:
    """Send SIGKILL to every subprocess CTA spawned (print mode + PTY mode).

    Only touches PIDs CTA tracks itself (`_current_procs`, `_backends`) so
    unrelated `claude` processes started by the user's terminal / IDE / other
    tools are never killed. Returns number of processes signalled.
    """
    n = 0
    # Print mode: each subprocess is in its own process group (start_new_session=True)
    for key, proc in list(_current_procs.items()):
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            n += 1
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
                n += 1
            except Exception:
                pass
        _current_procs.pop(key, None)
    # Backends own their own processes (PTY) and reader threads. Stop tears them down.
    for key, b in list(_backends.items()):
        try:
            b.stop()
            n += 1
        except Exception:
            pass
        _backends.pop(key, None)
    return n


def _install_shutdown_handler():
    """Install SIGTERM handler that kills tracked subprocesses before exit.

    launchctl sends SIGTERM before SIGKILL; this gives us a chance to clean up
    so CTA-spawned claude processes don't get re-parented to launchd as orphans.
    """
    def _handler(signum, frame):
        n = _kill_tracked_subprocs()
        print(f"[SHUTDOWN] killed {n} tracked subprocess(es) on signal {signum}", flush=True)
        # Re-raise default behaviour so the process actually exits.
        signal.signal(signum, signal.SIG_DFL)
        os.kill(os.getpid(), signum)
    signal.signal(signal.SIGTERM, _handler)
    signal.signal(signal.SIGINT, _handler)


def _polling_loop():
    """Run bot.infinity_polling in a loop, reconnecting after errors.

    Errno 49 ('can't assign address') and other transient network failures
    were silently killing the polling daemon thread. This wraps the call so
    the bot reconnects automatically after a 10-second backoff.
    """
    while True:
        try:
            bot.infinity_polling()
        except Exception as e:
            tui_log(f"[yellow]Polling error ({type(e).__name__}): {escape(str(e))} — retrying in 10s[/]")
            time.sleep(10)


# ── Claude CLI ────────────────────────────────────────────────────────────────

def _format_tokens_k(n: int) -> str:
    """Format a token count as a human-readable string (e.g. 17K, 1.2K, 42)."""
    if n < 1000:
        return str(n)
    if n < 10000:
        return f"{n/1000:.1f}K"
    return f"{n/1000:.0f}K"


def _append_usage_footer(text: str, data: dict) -> str:
    """Append a context-window + output-tokens footer to a Claude reply.

    Format: '— ctx: 17K/200K (8%) / out: 42'. The ctx number is total input
    tokens (fresh + cache_creation + cache_read) since all of it occupies the
    model's context window. Falls back to a simple in/out line if the JSON
    response is missing modelUsage.contextWindow (older claude-code versions).
    """
    usage = data.get("usage") or {}
    inp = (usage.get("input_tokens", 0)
           + usage.get("cache_creation_input_tokens", 0)
           + usage.get("cache_read_input_tokens", 0))
    out = usage.get("output_tokens", 0)
    if not inp and not out:
        return text
    ctx_window = max((m.get("contextWindow", 0) for m in (data.get("modelUsage") or {}).values()),
                     default=0)
    if ctx_window > 0:
        pct = inp / ctx_window * 100
        return f"{text}\n\n📊 ctx: {_format_tokens_k(inp)}/{_format_tokens_k(ctx_window)} ({pct:.0f}%) / out: {_format_tokens_k(out)}"
    return f"{text}\n\n📊 in: {_format_tokens_k(inp)} / out: {_format_tokens_k(out)}"


def call_claude(prompt: str, cwd: str = None, session_id: str = None, model: str = None,
                timeout: int = None,
                uid: int = None, chat_id: int = None) -> tuple[str, str]:
    """Call Claude Code CLI. Returns (text, session_id).

    When uid/chat_id are provided, sets CTA_UID/CTA_CHAT_ID in the subprocess env
    so cron.py (and any other helper scripts) can know which chat they're in.
    """
    cwd = cwd or DEFAULT_CWD
    debug_path = os.path.join(
        DEBUG_DIR,
        f"{uid or 'x'}-{chat_id or 'x'}-{int(time.time())}.log",
    )
    cmd = [CLAUDE_BIN, "--print", "--dangerously-skip-permissions",
           "--model", model or MODEL, "--output-format", "json",
           "--debug-file", debug_path, "-p", prompt]
    if session_id:
        cmd += ["--resume", session_id]
    env = os.environ.copy()
    if uid is not None:
        env["CTA_UID"] = str(uid)
    if chat_id is not None:
        env["CTA_CHAT_ID"] = str(chat_id)
    key = (uid, chat_id) if uid is not None and chat_id is not None else None
    label = chat_labels.get(key, f"{uid}:{chat_id}") if key else "—"
    # Concurrency gate. Capture a local ref BEFORE acquire so a runtime
    # config change that swaps the global semaphore can't make us release
    # a different instance from the one we acquired (would leak permits).
    sem = _claude_semaphore
    if MAX_CONCURRENT_CLAUDE > 1 or sem._value < 1:  # type: ignore[attr-defined]
        # Only log when we might actually wait, to avoid noise.
        tui_log(f"[dim]→ acquiring slot for {escape(label)} ({MAX_CONCURRENT_CLAUDE} max)[/]")
    sem.acquire()
    try:
        # If /cancel arrived while we were blocked at acquire, bail out before
        # spawning the subprocess — otherwise the user's cancel is silently
        # overridden by work that runs the moment a slot opens up.
        if key and key in _cancelled_keys:
            return "(cancelled)", ""
        tui_log(f"[blue]→ claude[/] {escape(label)} model={escape(model or MODEL)} chars={len(prompt)} session={'resume' if session_id else 'new'} debug={escape(debug_path)}")
        print(f"[POPEN] uid={uid} chat={chat_id} cmd={cmd[0]} debug={debug_path}", flush=True)
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True, cwd=cwd, env=env, start_new_session=True)
        except FileNotFoundError:
            return "(claude CLI not found — install @anthropic-ai/claude-code)", ""
        print(f"[POPEN_OK] uid={uid} chat={chat_id} pid={proc.pid}", flush=True)
        if key:
            _current_procs[key] = proc
        try:
            try:
                stdout, stderr = proc.communicate(timeout=timeout or TIMEOUT)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.communicate()
                tui_log(f"[yellow]⏱ claude timeout[/] {escape(label)}")
                return "(Claude timed out)", ""
        finally:
            if key:
                _current_procs.pop(key, None)
        if proc.returncode != 0 or not stdout.strip():
            return f"[Error] {stderr.strip()}" if stderr.strip() else "(empty response)", ""
        data = json.loads(stdout)
        text = (data.get("result") or "").strip() or "(empty response)"
        text = _append_usage_footer(text, data)
        return text, data.get("session_id", "")
    finally:
        sem.release()


# ── Backend dispatch ──────────────────────────────────────────────────────────

_MODE_TO_CLS = {
    "pty": "PtyBackend",
    "stream": "JsonStreamBackend",
    "print": "PrintBackend",
}


def _get_backend(key: tuple[int, int]) -> "backends.ClaudeBackend":
    """Return the live backend for this chat, swapping mode if toggled."""
    mode = user_backend_mode.get(key, "print")
    desired_cls = getattr(backends, _MODE_TO_CLS.get(mode, "PrintBackend"))
    b = _backends.get(key)
    if b is not None and isinstance(b, desired_cls):
        return b
    if b is not None:
        b.stop()
    uid, chat_id = key
    b = desired_cls(uid, chat_id)
    b.on_log = tui_log

    def _on_clear_session(k=key) -> None:
        user_sessions.pop(k, None)
        save_sessions()

    if isinstance(b, backends.PtyBackend):
        b.on_typing = lambda: bot.send_chat_action(chat_id, "typing")
        b.start_config = lambda: (
            user_cwd.get(key, DEFAULT_CWD),
            user_model.get(key, MODEL),
            user_sessions.get(key),
        )
        b.on_clear_session = _on_clear_session
    if isinstance(b, backends.JsonStreamBackend):
        b.on_typing = lambda: bot.send_chat_action(chat_id, "typing")
        b.on_clear_session = _on_clear_session
        def _on_session(sid: str, k=key) -> None:
            user_sessions[k] = sid
            save_sessions()
        b.on_session = _on_session
    _backends[key] = b
    return b


def _stop_backend(key: tuple[int, int]) -> None:
    """Stop and forget the chat's backend, if any."""
    b = _backends.pop(key, None)
    if b is not None:
        b.stop()


# ── Web chat history & SSE ────────────────────────────────────────────────────

_CHAT_HISTORY_MAX = 200

def _chat_push(uid: int, chat_id: int, role: str, text: str):
    """Append a message to per-chat history and broadcast to web chat SSE subscribers."""
    key = (uid, chat_id)
    now = time.time()
    last_active[key] = now
    entry = {"role": role, "text": text, "ts": now}
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



# ── Message processing ────────────────────────────────────────────────────────

def _build_preamble(uid: int, chat_id: int) -> str:
    """Build the prompt prefix injected before every agent turn.

    Includes the system identity block, any cron parse error warning,
    the global preamble, and the per-chat custom preamble.
    """
    crons_path = os.path.join(CRONS_DIR, f"{uid}:{chat_id}.json")
    _ensure_cron_file(uid, chat_id)
    crons_err = _crons_parse_error(uid, chat_id)
    if crons_err:
        tui_log(f"[red]⚠ crons parse error {uid}:{chat_id}: {escape(crons_err)}[/]")
    custom_preamble = _read_preamble(uid, chat_id)
    parts = [_system_preamble(uid, chat_id)]
    if crons_err:
        parts.append(
            f"⚠ Your crons file at {crons_path} is INVALID JSON ({crons_err}). "
            "Scheduled jobs in it will NOT run and are hidden from the UI. "
            "Fix the JSON syntax (likely unescaped quotes in a prompt string) "
            "so your crons work again."
        )
    if GLOBAL_PREAMBLE:
        parts.append(GLOBAL_PREAMBLE)
    if custom_preamble:
        parts.append(custom_preamble)
    return "\n".join(parts) + "\n"


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
    print(f"[PROCESS_MSG] uid={uid} chat={chat_id}", flush=True)
    key = (uid, chat_id)
    cwd = user_cwd.get(key, DEFAULT_CWD)
    username = message.from_user.username or str(uid)
    if message.chat.type == "private":
        chat_labels[key] = f"DM:{username}"
    else:
        chat_labels[key] = message.chat.title or str(chat_id)

    preamble = _build_preamble(uid, chat_id)
    caption = message.caption or ""
    prompt = preamble + (message.text or caption)
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
            prompt = preamble + f"Use the Read tool to read and analyze the file at: {tmp_photo}{user_instruction}"
        except Exception as e:
            tui_log(f"[red]⚠ file download failed: {escape(str(e))}[/]")
            bot.reply_to(message, f"❌ Could not download file: {e}")
            done.set()
            return
    elif message.photo:
        try:
            photo = message.photo[-1]  # highest resolution
            file_info = bot.get_file(photo.file_id)
            data = bot.download_file(file_info.file_path)
            ext = os.path.splitext(file_info.file_path)[1] or ".jpg"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(data)
            tmp.close()
            tmp_photo = tmp.name
            user_instruction = f"\n\nUser's question: {caption}" if caption else ""
            prompt = preamble + f"Use the Read tool to view the image at: {tmp_photo}{user_instruction}"
        except Exception as e:
            tui_log(f"[red]⚠ photo download failed: {escape(str(e))}[/]")
            bot.reply_to(message, f"❌ Could not download photo: {e}")
            done.set()
            return
    elif message.voice or message.audio:
        voice = message.voice or message.audio
        try:
            file_info = bot.get_file(voice.file_id)
            data = bot.download_file(file_info.file_path)
            ext = os.path.splitext(file_info.file_path)[1] or ".oga"
            tmp = tempfile.NamedTemporaryFile(delete=False, suffix=ext)
            tmp.write(data)
            tmp.close()
            tmp_photo = tmp.name
            bot.reply_to(message, "🎙 Transcribing…")
            whisper_model = _load_whisper()
            result = whisper_model.transcribe(tmp_photo)
            transcript = result["text"].strip()
            if not transcript:
                bot.reply_to(message, "❌ Could not transcribe voice message.")
                done.set()
                return
            tui_log(f"[cyan]🎙 transcript:[/] {escape(transcript)}")
            _chat_push(uid, message.chat.id, "user", f"🎙 {transcript}")
            prompt = preamble + transcript
        except ImportError:
            bot.reply_to(message, "❌ Whisper not installed. Run: pip install openai-whisper")
            done.set()
            return
        except FileNotFoundError as e:
            if "ffmpeg" in str(e):
                bot.reply_to(message, "❌ ffmpeg not found. Install it with: brew install ffmpeg")
            else:
                bot.reply_to(message, f"❌ Transcription failed: {e}")
            tui_log(f"[red]⚠ voice transcription failed: {escape(str(e))}[/]")
            done.set()
            return
        except Exception as e:
            tui_log(f"[red]⚠ voice transcription failed: {escape(str(e))}[/]")
            bot.reply_to(message, f"❌ Transcription failed: {e}")
            done.set()
            return

    mode = user_backend_mode.get(key, "print")
    print(f"[CALL_CLAUDE] uid={uid} chat={chat_id} model={user_model.get(key, MODEL)} cwd={cwd} mode={mode}", flush=True)
    backend = _get_backend(key)
    backend.on_output = _make_output_handler(uid, chat_id, message, username, mode)
    try:
        backend.send(prompt)
    except claude_code.ClaudeNotReady as e:
        bot.reply_to(message, f"❌ PTY not ready: {e}")
        _stop_backend(key)
    except Exception as e:
        tui_log(f"[red]⚠ backend send error for {key}: {escape(str(e))}[/]")
        bot.reply_to(message, f"❌ Backend error: {type(e).__name__}: {e}")
        _stop_backend(key)
    finally:
        done.set()
        if tmp_photo:
            os.unlink(tmp_photo)


def _make_output_handler(uid: int, chat_id: int, message, username: str, mode: str):
    """Build the callback a backend invokes for each chunk of new output.

    Centralises post-processing (history, msg_counts, persistence, Telegram
    fan-out) so backends only have to deliver text — they don't need to
    know about Telegram-specific formatting or chat-history bookkeeping.
    """
    key = (uid, chat_id)

    def on_output(text: str) -> None:
        if not text:
            return
        msg_counts[key] = msg_counts.get(key, 0) + 1
        last_reply[key] = text[:300]
        preview = text[:120].replace("\n", " ")
        tui_log(f"[blue]←[/] [bold]{escape(username)}[/] {escape(preview)}{'…' if len(text) > 120 else ''}")
        print(f"[CLAUDE_OUTPUT] uid={uid} chat={chat_id}\n{text}", flush=True)
        _chat_push(uid, chat_id, "assistant", text)
        save_sessions()
        if mode == "pty":
            # PTY output is raw terminal text; skip MarkdownV2 and reply_to.
            for chunk in _split_reply(text):
                try:
                    bot.send_message(chat_id, chunk)
                except Exception as e:
                    tui_log(f"[red]pty send error {key}: {escape(str(e))}[/]")
        elif mode == "stream":
            # stream-json output is proper markdown; skip reply_to but apply MarkdownV2.
            for chunk in _split_reply(text):
                try:
                    bot.send_message(chat_id, telegramify_markdown.markdownify(chunk), parse_mode="MarkdownV2")
                except Exception:
                    try:
                        bot.send_message(chat_id, chunk)
                    except Exception as e:
                        tui_log(f"[red]stream send error {key}: {escape(str(e))}[/]")
        else:
            for chunk in _split_reply(text):
                _send_markdown(message, chunk)

    return on_output


def _process_cron(uid: int, chat_id: int, task: dict, done: threading.Event):
    key = (uid, chat_id)
    cwd = user_cwd.get(key, DEFAULT_CWD)
    model = user_model.get(key, MODEL)
    timeout = user_timeout.get(key, TIMEOUT)
    session_id = user_sessions.get(key)
    job_id = task["job_id"]
    prompt = _build_preamble(uid, chat_id) + f"[Scheduled task {job_id}]\n{task['prompt']}"

    claude_active_keys.add(key)
    try:
        reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=session_id, model=model, timeout=timeout, uid=uid, chat_id=chat_id)
        if session_id and "No conversation found with session ID" in reply:
            user_sessions.pop(key, None)
            reply, new_session_id = call_claude(prompt, cwd=cwd, session_id=None, model=model, timeout=timeout, uid=uid, chat_id=chat_id)
    finally:
        claude_active_keys.discard(key)
        done.set()

    if key in _cancelled_keys:
        _cancelled_keys.discard(key)
        return
    if new_session_id:
        user_sessions[key] = new_session_id
    msg_counts[key] = msg_counts.get(key, 0) + 1
    last_reply[key] = reply[:300]
    preview = reply[:120].replace("\n", " ")
    tui_log(f"[magenta]←[/] [bold]cron:{job_id}[/] {escape(preview)}{'…' if len(reply) > 120 else ''}")
    _chat_push(uid, chat_id, "assistant", reply)
    save_sessions()
    for chunk in _split_reply(reply):
        bot.send_message(chat_id, telegramify_markdown.markdownify(chunk), parse_mode="MarkdownV2")


def _is_plain_text(item) -> bool:
    """Return True if item is a plain text message that can be batched."""
    return (
        not isinstance(item, dict)
        and getattr(item, "text", None)
        and not getattr(item, "document", None)
        and not getattr(item, "voice", None)
        and not getattr(item, "audio", None)
        and not getattr(item, "photo", None)
    )


def _user_worker(uid: int, chat_id: int, q: queue.Queue):
    while True:
        item = q.get()
        print(f"[WORKER_DEQUEUE] uid={uid} chat={chat_id} type={'cron' if isinstance(item, dict) else 'msg'}", flush=True)
        done = threading.Event()
        threading.Thread(target=_typing_loop, args=(chat_id, done), daemon=True).start()
        try:
            if isinstance(item, dict) and item.get("_type") == "cron":
                _process_cron(uid, chat_id, item, done)
            else:
                if _is_plain_text(item):
                    extra_texts = []
                    while True:
                        try:
                            extra = q.get_nowait()
                            if _is_plain_text(extra):
                                extra_texts.append(extra.text)
                                q.task_done()
                            else:
                                with q.mutex:
                                    q.queue.appendleft(extra)
                                    q.not_empty.notify()
                                break
                        except queue.Empty:
                            break
                    if extra_texts:
                        all_texts = [item.text] + extra_texts
                        item.text = "\n\n".join(
                            f"[Message {i+1}]: {t}" for i, t in enumerate(all_texts)
                        )
                        tui_log(f"[cyan]batched {len(all_texts)} messages for {uid}:{chat_id}[/]")
                print(f"[WORKER_DISPATCH] uid={uid} chat={chat_id} calling _process_message", flush=True)
                _process_message(uid, chat_id, item, done)
        except Exception as e:
            done.set()
            tui_log(f"[red][worker:{uid}:{chat_id}] error: {escape(str(e))}[/]")
            try:
                bot.send_message(chat_id, f"❌ Internal error: {e}")
            except Exception:
                pass
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
    print(f"[CMD_HELP] entered chat={message.chat.id} text={message.text!r}", flush=True)
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
        "/opus — switch to latest Opus model (clears session)\n"
        "/sonnet — switch to latest Sonnet model (clears session)\n"
        "/timeout `<seconds>` — set per-chat Claude timeout (or `reset`)\n"
        "/backend `print|stream|pty|status` — switch backend (default: print)\n"
        "/status — show model, cwd, timeout, and session info"
    ), parse_mode="Markdown")


def cmd_clear(message):
    if not _allowed(message): return
    key = (message.from_user.id, message.chat.id)
    user_sessions.pop(key, None)
    _stop_backend(key)
    save_sessions()
    bot.reply_to(message, "🧹 Conversation cleared.")


def cmd_cancel(message):
    if not _allowed(message): return
    uid = message.from_user.id
    key = (uid, message.chat.id)
    parts = []
    backend = _backends.get(key)
    if backend is not None and backend.cancel():
        parts.append("current task stopped")
        print(f"[CANCEL] backend cancel key={key} type={type(backend).__name__}", flush=True)
    elif key in claude_active_keys:
        # Worker is in-flight but no subprocess yet — likely blocked at the
        # semaphore acquire when concurrency is saturated. Mark it cancelled
        # so call_claude bails out as soon as a slot opens.
        _cancelled_keys.add(key)
        parts.append("queued task will be cancelled when slot opens")
        print(f"[CANCEL] mark queued key={key}", flush=True)
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
    key = (uid, message.chat.id)
    user_cwd[key] = expanded
    user_sessions.pop(key, None)
    _stop_backend(key)
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
    key = (uid, message.chat.id)
    user_model[key] = name
    _stop_backend(key)
    save_sessions()
    bot.reply_to(message, f"🤖 Model → `{name}`", parse_mode="Markdown")


def cmd_opus(message):
    print(f"[CMD_OPUS] entered chat={message.chat.id} user={message.from_user.id} text={message.text!r}", flush=True)
    if not _allowed(message):
        print(f"[CMD_OPUS] denied", flush=True)
        return
    uid = message.from_user.id
    name = "claude-opus-4-7"
    key = (uid, message.chat.id)
    user_model[key] = name
    _stop_backend(key)
    save_sessions()
    try:
        bot.reply_to(message, f"🤖 Model → `{name}`", parse_mode="Markdown")
        print(f"[CMD_OPUS] reply_to OK", flush=True)
    except Exception as e:
        print(f"[CMD_OPUS] reply_to FAILED: {e!r}", flush=True)


def cmd_sonnet(message):
    print(f"[CMD_SONNET] entered chat={message.chat.id} user={message.from_user.id} text={message.text!r}", flush=True)
    if not _allowed(message):
        print(f"[CMD_SONNET] denied", flush=True)
        return
    uid = message.from_user.id
    name = "claude-sonnet-4-6"
    key = (uid, message.chat.id)
    user_model[key] = name
    _stop_backend(key)
    save_sessions()
    try:
        bot.reply_to(message, f"🤖 Model → `{name}`", parse_mode="Markdown")
        print(f"[CMD_SONNET] reply_to OK", flush=True)
    except Exception as e:
        print(f"[CMD_SONNET] reply_to FAILED: {e!r}", flush=True)


_MODE_LABELS = {
    "print": "print (`claude --print`)",
    "stream": "stream (`--output-format stream-json`)",
    "pty": "pty (ClaudeCode PTY wrapper)",
}


def cmd_backend(message):
    """/backend [print|stream|pty|status] — switch the claude backend for this chat."""
    if not _allowed(message): return
    uid = message.from_user.id
    key = (uid, message.chat.id)
    arg = message.text.replace("/backend", "", 1).strip().lower()
    if arg in ("", "status"):
        mode = user_backend_mode.get(key, "print")
        bot.reply_to(message, f"Backend: `{mode}`", parse_mode="Markdown")
        return
    if arg in ("print", "stream", "pty"):
        if arg == "print":
            user_backend_mode.pop(key, None)
        else:
            user_backend_mode[key] = arg
        _stop_backend(key)
        save_sessions()
        bot.reply_to(message, f"Backend → {_MODE_LABELS[arg]}", parse_mode="Markdown")
        return
    bot.reply_to(message, "Usage: `/backend print|stream|pty|status`", parse_mode="Markdown")


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
    print(f"[HANDLE_MSG] chat={message.chat.id} user={message.from_user.id} text={message.text!r}", flush=True)
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


def handle_photo(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        source = "[DM]"
    else:
        source = f"[Group: {escape(message.chat.title or str(message.chat.id))}]"
    tui_log(f"[green]→[/] {source} [bold]{escape(str(message.from_user.username or uid))}[/] [dim]🖼 photo[/]")
    if not _allowed(message):
        return
    _get_user_queue(uid, message.chat.id).put(message)


def handle_voice(message):
    uid = message.from_user.id
    if message.chat.type == "private":
        source = "[DM]"
    else:
        source = f"[Group: {escape(message.chat.title or str(message.chat.id))}]"
    tui_log(f"[green]→[/] {source} [bold]{escape(str(message.from_user.username or uid))}[/] [dim]🎙 voice[/]")
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
    bot.message_handler(commands=["opus"])(cmd_opus)
    bot.message_handler(commands=["sonnet"])(cmd_sonnet)
    bot.message_handler(commands=["backend"])(cmd_backend)
    bot.message_handler(commands=["timeout"])(cmd_timeout)
    bot.message_handler(commands=["status"])(cmd_status)
    bot.message_handler(func=lambda m: m.text and not m.text.startswith("/"))(handle_message)
    bot.message_handler(content_types=["document"])(handle_document)
    bot.message_handler(content_types=["photo"])(handle_photo)
    bot.message_handler(content_types=["voice", "audio"])(handle_voice)
    return bot


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    config = load_config()
    init(config)
    web.init(sys.modules[__name__])

    if not BOT_TOKEN:
        print(f"Error: telegram_bot_token not set in {CONFIG_PATH}")
        exit(1)

    _install_shutdown_handler()
    load_sessions()
    create_bot()
    tui_log(f"[dim]CTA starting… model=[cyan]{MODEL}[/] cwd=[cyan]{DEFAULT_CWD}[/] users={ALLOWED_USERS or 'all'}[/]")

    for uid in ALLOWED_USERS or set(user_sessions.keys()):
        try:
            bot.send_message(uid, "✅ CTA is ready.")
        except Exception as e:
            tui_log(f"[red]Could not notify {uid}: {escape(str(e))}[/]")

    threading.Thread(target=_polling_loop, daemon=True).start()
    threading.Thread(target=_cron_scheduler, daemon=True).start()
    tui_log(f"[dim]Web UI → http://localhost:{WEB_PORT}/[/]")
    app.run(host="0.0.0.0", port=WEB_PORT, threaded=True, debug=False, use_reloader=False)
