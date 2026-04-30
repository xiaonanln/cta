"""ClaudeBackend abstract base — the contract every execution mode satisfies."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Callable, Optional


class ClaudeBackend(ABC):
    """Per-chat handle for executing Claude prompts.

    Subclasses encapsulate one execution mode and stream output back via
    the ``on_output`` callback. ``send`` may block (synchronous modes like
    print) or return immediately (async modes like PTY); callers must not
    assume blocking semantics.
    """

    def __init__(self, uid: int, chat_id: int):
        self.uid = uid
        self.chat_id = chat_id
        self.on_output: Optional[Callable[[str], None]] = None

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
