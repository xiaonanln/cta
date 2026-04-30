"""Per-chat Claude execution backends.

Each backend wraps one execution mode (`claude --print`, PTY interactive,
or — in the future — `--print --output-format=stream-json`) behind a
uniform `ClaudeBackend.send(prompt)` interface so `agent.py` doesn't have
to branch on mode for every operation.
"""

from .base import ClaudeBackend
from .print_mode import PrintBackend
from .pty import PtyBackend

__all__ = ["ClaudeBackend", "PrintBackend", "PtyBackend"]
