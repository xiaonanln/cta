"""PtyBackend — long-lived `claude` interactive subprocess driven via the TUI.

Output is decoupled from input: `send` just writes to the PTY and returns.
A per-backend reader thread streams new screen content back to ``on_output``.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Optional

import claude_code

from .base import ClaudeBackend

_TYPING_IDLE_SECONDS = 3.0   # stop typing after this many seconds without output
_TYPING_PULSE_SECONDS = 3.0  # re-send typing action this often (Telegram expires after ~5s)
_OUTPUT_COALESCE_SECONDS = 3.0  # buffer lines and flush after this many seconds of quiet (noise frames don't reset the timer)
_now = time.time  # indirection so tests can patch the reader_loop clock without touching the real time module


class PtyBackend(ClaudeBackend):
    def __init__(self, uid: int, chat_id: int):
        super().__init__(uid, chat_id)
        self._cc: claude_code.ClaudeCode | None = None
        self._reader: threading.Thread | None = None
        self._stop_event: threading.Event | None = None
        self._last_activity: float = 0.0
        self._typing_stop: threading.Event | None = None
        self._typing_thread: threading.Thread | None = None
        # Set by agent to supply (cwd, model) at PTY start time without
        # the backend needing to import agent.
        self.start_config: Optional[Callable[[], tuple[str, str]]] = None

    @property
    def cc(self) -> claude_code.ClaudeCode | None:
        """Live ClaudeCode handle, or None if not started / torn down. Tests use this."""
        return self._cc

    @property
    def is_running(self) -> bool:
        return (self._cc is not None
                and self._cc.proc is not None
                and self._cc.proc.poll() is None)

    def _log(self, msg: str) -> None:
        if self.on_log:
            self.on_log(msg)
        else:
            print(msg, flush=True)

    def _ensure_started(self) -> None:
        if self.is_running:
            return
        # Dead-but-cached instance — clean up before respawning so master_fd
        # and the old reader thread don't leak.
        if self._cc is not None:
            self._teardown()
        cwd, model = self.start_config() if self.start_config else ("", "")
        cc = claude_code.ClaudeCode(
            cwd=cwd, model=model,
            extra_env={"CTA_UID": str(self.uid), "CTA_CHAT_ID": str(self.chat_id)},
        )
        print(f"[PTY] spawning ClaudeCode for {self.key} cwd={cwd} model={model}", flush=True)
        try:
            cc.start(ready_timeout=45)
        except Exception:
            try:
                cc.stop()
            except Exception:
                pass
            raise
        self._cc = cc
        self._stop_event = threading.Event()
        self._reader = threading.Thread(
            target=self._reader_loop,
            name=f"pty-reader:{self.uid}:{self.chat_id}",
            daemon=True,
        )
        self._reader.start()
        print(f"[PTY] ready for {self.key}", flush=True)

    def _reader_loop(self) -> None:
        """Long-lived: read new lines from the PTY, coalesce them within a
        quiet window, and forward via on_output. Lines accumulate until no
        real content has arrived for `_OUTPUT_COALESCE_SECONDS` — noise
        frames (filtered out by `read_new_output`) don't reset the timer."""
        cc = self._cc
        stop_event = self._stop_event
        pending: list[str] = []
        last_content_time = 0.0

        def flush() -> None:
            nonlocal pending
            if not pending:
                return
            text = "\n".join(pending).strip()
            pending = []
            if not text:
                return
            print(f"[PTY_OUTPUT] uid={self.uid} chat={self.chat_id}\n{text}", flush=True)
            cb = self.on_output
            if cb is None:
                return
            try:
                cb(text)
            except Exception as e:
                self._log(f"[red]pty reader on_output error {self.key}: {e}[/]")

        while not stop_event.is_set():
            if cc.proc is None or cc.proc.poll() is not None:
                break
            try:
                new_lines = cc.read_new_output(timeout=0.5)
            except Exception as e:
                self._log(f"[red]pty reader read error {self.key}: {e}[/]")
                break
            # Sync activity from raw PTY bytes — covers noise/redraw-only frames
            # where new_lines is empty but Claude is still actively generating.
            self._last_activity = cc.last_pty_bytes
            if new_lines:
                pending.extend(new_lines)
                last_content_time = _now()
            elif pending and _now() - last_content_time >= _OUTPUT_COALESCE_SECONDS:
                flush()
        flush()

    def send(self, prompt: str) -> None:
        self._ensure_started()
        self._last_activity = time.time()
        self._start_typing()
        self._cc.send_input(prompt)

    def _start_typing(self) -> None:
        if self._typing_stop is not None:
            self._typing_stop.set()
        stop = threading.Event()
        self._typing_stop = stop
        t = threading.Thread(
            target=self._typing_loop,
            args=(stop,),
            name=f"pty-typing:{self.uid}:{self.chat_id}",
            daemon=True,
        )
        t.start()
        self._typing_thread = t

    def _typing_loop(self, stop: threading.Event) -> None:
        def pulse() -> None:
            if self.on_typing:
                try:
                    self.on_typing()
                except Exception:
                    pass

        pulse()
        while not stop.wait(timeout=_TYPING_PULSE_SECONDS):
            if time.time() - self._last_activity > _TYPING_IDLE_SECONDS:
                break
            cc = self._cc
            if cc and cc.is_idle():
                break
            pulse()

    def cancel(self) -> bool:
        if self._cc is None:
            return False
        try:
            self._cc.cancel()
            return True
        except Exception:
            return False

    def stop(self) -> None:
        self._teardown()

    def _teardown(self) -> None:
        if self._typing_stop is not None:
            self._typing_stop.set()
        self._typing_stop = None
        self._typing_thread = None
        if self._stop_event is not None:
            self._stop_event.set()
        if self._reader is not None:
            self._reader.join(timeout=2)
        if self._cc is not None:
            print(f"[PTY] stopping ClaudeCode for {self.key}", flush=True)
            try:
                self._cc.stop()
            except Exception as e:
                self._log(f"[red]⚠ pty stop error for {self.key}: {e}[/]")
        self._cc = None
        self._reader = None
        self._stop_event = None
