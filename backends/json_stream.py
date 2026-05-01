"""JsonStreamBackend — `claude --print --output-format stream-json`.

One subprocess per send() call (like PrintBackend) but delivers text chunks
in real-time via on_output (like PtyBackend). Simpler than PTY: no screen-
scraping, no pyte, no noise filtering — just newline-delimited JSON events.

ClaudeJsonStream is the testable seam: tests mock it the same way PtyBackend
tests mock ClaudeCode.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import claude_json_stream as _cjs_mod

from .base import ClaudeBackend

_COALESCE_SECONDS = 3.0
_TYPING_PULSE_SECONDS = 3.0
_INVALID_SESSION_MSG = 'No conversation found with session ID'


class JsonStreamBackend(ClaudeBackend):
    def __init__(self, uid: int, chat_id: int):
        super().__init__(uid, chat_id)
        self._stream: Optional[_cjs_mod.ClaudeJsonStream] = None
        self._stream_lock = threading.Lock()
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

        import os, time as _time
        debug_path = os.path.join(
            agent.DEBUG_DIR,
            f'{self.uid}-{self.chat_id}-{int(_time.time())}.log',
        )

        agent.claude_active_keys.add(key)
        sem = agent._claude_semaphore
        sem.acquire()
        try:
            if key in agent._cancelled_keys:
                agent._cancelled_keys.discard(key)
                return
            # Retry once if claude rejects the session_id as stale.
            for attempt in range(2):
                stream = _cjs_mod.ClaudeJsonStream(
                    prompt=prompt,
                    cwd=cwd,
                    model=model,
                    session_id=session_id,
                    claude_bin=agent.CLAUDE_BIN,
                    debug_log=debug_path,
                    extra_env={'CTA_UID': str(self.uid), 'CTA_CHAT_ID': str(self.chat_id)},
                )
                print(
                    f'[STREAM] uid={self.uid} chat={self.chat_id} '
                    f'model={model} session={"resume" if session_id else "new"} '
                    f'debug={debug_path}',
                    flush=True,
                )
                try:
                    stream.start()
                except FileNotFoundError:
                    if self.on_output:
                        self.on_output('(claude CLI not found — install @anthropic-ai/claude-code)')
                    return
                with self._stream_lock:
                    self._stream = stream
                agent._current_procs[key] = stream.proc
                if attempt == 0:
                    self._start_typing()
                try:
                    invalid_session = self._run_reader(
                        stream, timeout or agent.TIMEOUT, key,
                        had_session=bool(session_id),
                    )
                finally:
                    agent._current_procs.pop(key, None)
                    with self._stream_lock:
                        if self._stream is stream:
                            self._stream = None
                if not invalid_session or not session_id:
                    break
                # Stale session — clear it and retry without --resume.
                print(
                    f'[STREAM] invalid session {session_id} for {key}, retrying without resume',
                    flush=True,
                )
                agent.user_sessions.pop(key, None)
                agent.save_sessions()
                session_id = None
        finally:
            self._stop_typing()
            # Consume the cancel flag so the next prompt isn't silently dropped.
            agent._cancelled_keys.discard(key)
            sem.release()
            agent.claude_active_keys.discard(key)

    def _run_reader(
        self,
        stream: '_cjs_mod.ClaudeJsonStream',
        timeout: int,
        key: tuple,
        had_session: bool = False,
    ) -> bool:
        """Read events until EOF or result. Returns True if the session ID was rejected.

        Invalid-session detection: claude prints the error to stderr (devnull'd) and
        emits a result event with is_error=True, num_turns=0, and an empty result
        string. We treat this combination as a stale session only when had_session=True
        to avoid retrying on unrelated startup failures.
        """
        import agent

        deadline = time.time() + timeout
        pending: list[str] = []
        last_text_time: float = 0.0  # 0 means no text received yet

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            if key in agent._cancelled_keys:
                pending.clear()
                return
            text = ''.join(pending).strip()
            pending.clear()
            if text and self.on_output:
                self.on_output(text)

        for event in stream.iter_events():
            if time.time() > deadline:
                stream.stop()
                if self.on_output:
                    self.on_output('(Claude timed out)')
                return False

            etype = event.get('type')
            if etype == 'stream_event':
                delta = _extract_text_delta(event)
                if delta:
                    now = time.time()
                    # Flush accumulated text if Claude paused (e.g. during tool use).
                    if last_text_time and now - last_text_time >= _COALESCE_SECONDS:
                        flush()
                    pending.append(delta)
                    last_text_time = now
            elif etype == 'result':
                flush()
                sid = event.get('session_id', '')
                if sid and self.on_session:
                    self.on_session(sid)
                if event.get('is_error') or event.get('subtype') == 'error':
                    msg = (event.get('result') or '').strip()
                    # Invalid session: claude writes the error to stderr (devnull'd)
                    # and emits is_error=True with num_turns=0 and an empty result.
                    if had_session and not msg and event.get('num_turns', -1) == 0:
                        return True
                    if self.on_output:
                        self.on_output(msg or '(error)')
                return False

        flush()
        return False

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

        with self._stream_lock:
            stream = self._stream
        if stream is None:
            key = self.key
            if key in agent.claude_active_keys:
                agent._cancelled_keys.add(key)
                return True
            return False
        try:
            stream.stop()
        except Exception:
            return False
        agent._cancelled_keys.add(self.key)
        return True

    def stop(self) -> None:
        self._stop_typing()
        with self._stream_lock:
            stream = self._stream
        if stream is not None:
            stream.stop()


def _extract_text_delta(event: dict) -> str:
    """Extract new text from a stream_event content_block_delta event."""
    inner = event.get('event') or {}
    if inner.get('type') != 'content_block_delta':
        return ''
    delta = inner.get('delta') or {}
    if delta.get('type') != 'text_delta':
        return ''
    return delta.get('text', '')
