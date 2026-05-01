"""JsonStreamBackend — `claude --print --output-format stream-json`.

One subprocess per send() call (like PrintBackend) but delivers text chunks
in real-time via on_output (like PtyBackend). Simpler than PTY: no screen-
scraping, no pyte, no noise filtering — just newline-delimited JSON events.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import threading
import time
from typing import Callable, Optional

from .base import ClaudeBackend

_COALESCE_SECONDS = 3.0
_TYPING_PULSE_SECONDS = 3.0


class JsonStreamBackend(ClaudeBackend):
    def __init__(self, uid: int, chat_id: int):
        super().__init__(uid, chat_id)
        self._proc: Optional[subprocess.Popen] = None
        self._proc_lock = threading.Lock()
        self._typing_stop: Optional[threading.Event] = None
        # Called with the new session_id when the result event arrives.
        self.on_session: Optional[Callable[[str], None]] = None

    def send(self, prompt: str) -> None:
        import agent

        key = self.key
        cwd = agent.user_cwd.get(key, agent.DEFAULT_CWD)
        model = agent.user_model.get(key, agent.MODEL)
        timeout = agent.user_timeout.get(key, agent.TIMEOUT)
        session_id = agent.user_sessions.get(key)

        debug_path = os.path.join(
            agent.DEBUG_DIR,
            f"{self.uid}-{self.chat_id}-{int(time.time())}.log",
        )
        cmd = [
            agent.CLAUDE_BIN,
            '--print', '--dangerously-skip-permissions',
            '--output-format', 'stream-json',
            '--include-partial-messages',
            '--model', model,
            '--debug-file', debug_path,
            '-p', prompt,
        ]
        if session_id:
            cmd += ['--resume', session_id]

        env = os.environ.copy()
        env['CTA_UID'] = str(self.uid)
        env['CTA_CHAT_ID'] = str(self.chat_id)

        agent.claude_active_keys.add(key)
        sem = agent._claude_semaphore
        sem.acquire()
        try:
            if key in agent._cancelled_keys:
                agent._cancelled_keys.discard(key)
                return
            print(
                f"[STREAM] uid={self.uid} chat={self.chat_id} "
                f"model={model} session={'resume' if session_id else 'new'} "
                f"debug={debug_path}",
                flush=True,
            )
            try:
                proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    stdin=subprocess.DEVNULL,
                    text=True,
                    cwd=cwd,
                    env=env,
                    start_new_session=True,
                )
            except FileNotFoundError:
                if self.on_output:
                    self.on_output("(claude CLI not found — install @anthropic-ai/claude-code)")
                return
            with self._proc_lock:
                self._proc = proc
            agent._current_procs[key] = proc
            self._start_typing()
            try:
                self._run_reader(proc, timeout or agent.TIMEOUT, key)
            finally:
                agent._current_procs.pop(key, None)
                with self._proc_lock:
                    if self._proc is proc:
                        self._proc = None
                self._stop_typing()
        finally:
            sem.release()
            agent.claude_active_keys.discard(key)

    def _run_reader(self, proc: subprocess.Popen, timeout: int, key: tuple) -> None:
        import agent

        deadline = time.time() + timeout
        pending: list[str] = []
        last_flush = time.time()
        last_text: dict[str, str] = {}  # msg_id → cumulative text seen so far

        def flush() -> None:
            nonlocal pending, last_flush
            if not pending:
                return
            if key in agent._cancelled_keys:
                pending.clear()
                return
            text = '\n'.join(pending).strip()
            pending.clear()
            last_flush = time.time()
            if text and self.on_output:
                self.on_output(text)

        for raw in iter(proc.stdout.readline, ''):
            if time.time() > deadline:
                proc.kill()
                proc.communicate()
                if self.on_output:
                    self.on_output('(Claude timed out)')
                return

            line = raw.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue

            etype = event.get('type')
            if etype == 'assistant':
                delta = self._extract_delta(event, last_text)
                if delta:
                    pending.append(delta)
                    if time.time() - last_flush >= _COALESCE_SECONDS:
                        flush()
            elif etype == 'result':
                flush()
                sid = event.get('session_id', '')
                if sid and self.on_session:
                    self.on_session(sid)
                if event.get('is_error') or event.get('subtype') == 'error':
                    msg = (event.get('result') or '').strip() or '(error)'
                    if self.on_output:
                        self.on_output(msg)
                return

        proc.wait(timeout=5)
        flush()

    @staticmethod
    def _extract_delta(event: dict, last_text: dict[str, str]) -> str:
        """Return only the new text in this partial-message event.

        With --include-partial-messages, each assistant event is a cumulative
        snapshot of the message so far. We diff against last_text to get the
        delta so we don't re-emit already-seen content.
        """
        msg = event.get('message') or {}
        msg_id = msg.get('id', '')
        full = ''.join(
            block.get('text', '')
            for block in (msg.get('content') or [])
            if block.get('type') == 'text'
        )
        prev = last_text.get(msg_id, '')
        delta = full[len(prev):]
        if msg_id:
            last_text[msg_id] = full
        return delta

    def _start_typing(self) -> None:
        if self.on_typing is None:
            return
        stop = threading.Event()
        self._typing_stop = stop

        def loop() -> None:
            try:
                if self.on_typing:
                    self.on_typing()
            except Exception:
                pass
            while not stop.wait(timeout=_TYPING_PULSE_SECONDS):
                try:
                    if self.on_typing:
                        self.on_typing()
                except Exception:
                    pass

        threading.Thread(
            target=loop,
            daemon=True,
            name=f'stream-typing:{self.uid}:{self.chat_id}',
        ).start()

    def _stop_typing(self) -> None:
        stop = self._typing_stop
        if stop:
            stop.set()
        self._typing_stop = None

    def cancel(self) -> bool:
        import agent

        with self._proc_lock:
            proc = self._proc
        if proc is None:
            key = self.key
            if key in agent.claude_active_keys:
                agent._cancelled_keys.add(key)
                return True
            return False
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            try:
                proc.kill()
            except Exception:
                return False
        agent._cancelled_keys.add(self.key)
        return True

    def stop(self) -> None:
        self._stop_typing()
        with self._proc_lock:
            proc = self._proc
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except Exception:
                    pass
