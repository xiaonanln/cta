"""ClaudeBackend abstract base — the contract every execution mode satisfies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class ClaudeBackend(ABC):
    """Per-chat handle for executing Claude prompts.

    Subclasses encapsulate one execution mode and stream output back via
    the ``on_output`` callback. ``send`` may block (synchronous modes like
    print/stream) or return immediately (async modes like PTY); callers must
    not assume blocking semantics.

    Callbacks (all optional, set by agent before calling send):
      on_output(text)      — new text chunk ready to deliver
      on_typing()          — pulse Telegram "typing" indicator
      on_log(msg)          — log a message to the TUI
      on_session(sid)      — new Claude session_id arrived; persist it
      on_clear_session()   — stale session detected; clear the stored id
    """

    def __init__(self, uid: int, chat_id: int):
        self.uid = uid
        self.chat_id = chat_id
        self.on_output: Optional[Callable[[str], None]] = None
        self.on_typing: Optional[Callable[[], None]] = None
        self.on_log: Optional[Callable[[str], None]] = None
        self.on_session: Optional[Callable[[str], None]] = None
        self.on_clear_session: Optional[Callable[[], None]] = None

    @property
    def key(self) -> tuple[int, int]:
        return (self.uid, self.chat_id)

    @abstractmethod
    def send(self, prompt: str) -> None:
        """Submit a prompt. Output is delivered via ``self.on_output`` (one or more times)."""

    def cancel(self) -> bool:
        """Interrupt in-flight work. Returns True if there was something to cancel."""
        return False

    def stop(self) -> None:
        """Tear down per-backend resources. Idempotent; safe to call when never started."""
        return None
