"""
ClaudeCode — long-lived `claude` interactive process under a PTY.

Streaming I/O model: caller writes input whenever, reads new output as it
arrives. No "request → response" boundary; claude's TUI is bidirectional.

Usage:
    cc = ClaudeCode(cwd="/path/to/project", model="claude-sonnet-4-6")
    cc.start()
    cc.send_input("run ls in current dir")
    while alive:
        new_lines = cc.read_new_output(timeout=0.5)
        for line in new_lines:
            relay_to_telegram(line)
        # caller decides when to stop based on its own logic
        # (idle window, deadline, user cancelled, ...)
    cc.stop()

`read_new_output` returns only lines NOT previously yielded. It filters
out the input box, status bar, and the thinking footer so callers can
pipe straight to a UI without seeing TUI furniture.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import shutil
import signal
import struct
import subprocess
import termios
import time

import pyte

# Strip ANSI escape sequences (CSI, OSC, charset selection, simple ESC X).
_ANSI_RE = re.compile(
    r'\x1b\[[0-9;?]*[a-zA-Z]'      # CSI ... letter
    r'|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)'  # OSC ... BEL or ST
    r'|\x1b[=>]'                    # keypad mode
    r'|\x1b\([AB012]'                # G0 charset
    r'|\x1b[NOMc]'                   # single-shift / RIS
)


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub('', text)


class ClaudeNotReady(Exception):
    pass


class ClaudeStalled(Exception):
    pass


class ClaudeCode:
    """One persistent `claude` interactive process under a PTY."""

    def __init__(
        self,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        claude_bin: str | None = None,
        debug_log: str | None = None,
        rows: int = 40,
        cols: int = 120,
        extra_env: dict | None = None,
    ):
        self.cwd = cwd
        self.model = model
        self.session_id = session_id
        self.extra_env = dict(extra_env) if extra_env else {}
        self.claude_bin = (
            claude_bin
            or shutil.which('claude')
            or os.path.expanduser('~/.local/bin/claude')
        )
        # Always write debug logs — agreed convention.
        if debug_log is None:
            debug_dir = os.path.expanduser('~/.cta/debug')
            os.makedirs(debug_dir, exist_ok=True)
            debug_log = os.path.join(debug_dir, f'claudecode-{int(time.time())}.log')
        self.debug_log = debug_log
        self.rows = rows
        self.cols = cols
        self.proc: subprocess.Popen | None = None
        self.master_fd: int | None = None
        self._buffer_raw = ''     # accumulated raw output (with ANSI)
        self._buffer_clean = ''   # stripped, used for prompt detection
        # pyte virtual terminal — maintains the actual rendered screen
        # state so we can read the final, post-redraw content rather than
        # the accumulated stream of frames.
        self._screen = pyte.Screen(self.cols, self.rows)
        self._stream = pyte.ByteStream(self._screen)
        # Hashes of lines we've already returned via read_new_output, so
        # we don't re-emit them on every screen redraw. Reset on start().
        self._yielded_line_hashes: set[int] = set()
        # Timestamp of the last raw PTY bytes received (any bytes, including
        # noise/redraws). Used by PtyBackend to keep the typing indicator alive
        # during long tool phases that produce only filtered-out screen churn.
        self.last_pty_bytes: float = 0.0

    # ── lifecycle ────────────────────────────────────────────────────────
    def _build_cmd(self) -> list[str]:
        # `-c` continues the most recent conversation in `cwd`. We use it
        # (instead of `--resume <id>`) so a PTY can pick up where the print
        # backend left off — the session_id stored by print mode is the most
        # recent transcript in that cwd, so `-c` lands on it. Treating
        # session_id as a boolean trigger sidesteps the print-vs-PTY
        # session-id divergence that interactive mode introduces.
        cmd = [self.claude_bin, '--dangerously-skip-permissions']
        if self.model:
            cmd += ['--model', self.model]
        if self.session_id:
            cmd += ['-c']
        if self.debug_log:
            cmd += ['--debug-file', self.debug_log]
        return cmd

    def start(self, ready_timeout: float = 30.0) -> None:
        # Fresh process → fresh dedup state.
        self._yielded_line_hashes = set()
        cmd = self._build_cmd()

        master_fd, slave_fd = pty.openpty()
        # Set window size BEFORE the child is spawned — many TUIs query
        # this on startup and stall if it's 0×0.
        winsize = struct.pack('HHHH', self.rows, self.cols, 0, 0)
        fcntl.ioctl(slave_fd, termios.TIOCSWINSZ, winsize)
        # Build a minimal env that doesn't carry over any CLAUDE_* or
        # AI_* signals an outer agent might have set (which cause the
        # inner claude to detect "I'm inside another agent" and refuse to
        # draw its TUI). Only pass what claude needs to function.
        env = {
            'TERM': 'xterm-256color',
            'COLORTERM': 'truecolor',
            'COLUMNS': str(self.cols),
            'LINES': str(self.rows),
            'HOME': os.environ.get('HOME', '/Users/alex'),
            'USER': os.environ.get('USER', 'alex'),
            'PATH': os.environ.get('PATH', '/usr/bin:/bin:/usr/sbin:/sbin'),
            'SHELL': os.environ.get('SHELL', '/bin/zsh'),
            'LANG': os.environ.get('LANG', 'en_US.UTF-8'),
            'LC_ALL': os.environ.get('LC_ALL', os.environ.get('LANG', 'en_US.UTF-8')),
        }
        # Pass through SSH_AUTH_SOCK if present (used by some plugins)
        if 'SSH_AUTH_SOCK' in os.environ:
            env['SSH_AUTH_SOCK'] = os.environ['SSH_AUTH_SOCK']
        # Caller-supplied additions (e.g. CTA_UID / CTA_CHAT_ID so cron.py /
        # notify.py invoked from inside the chat know which chat they're in).
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            cmd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            cwd=self.cwd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        self.master_fd = master_fd
        # claude only draws its initial TUI frame after the first SIGWINCH.
        # Without this kick the pty receives ~51 bytes of terminal-setup
        # escapes and then nothing. Drain the initial setup bytes first so
        # claude has hit its main loop before we send the signal.
        deadline = time.time() + 3.0
        while time.time() < deadline:
            self._read_chunk(0.3)
        try:
            # Re-set the size on master to ensure SIGWINCH actually fires
            # (kernel only delivers SIGWINCH if the size *changes*).
            small = struct.pack('HHHH', self.rows - 1, self.cols, 0, 0)
            full = struct.pack('HHHH', self.rows, self.cols, 0, 0)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, small)
            fcntl.ioctl(self.master_fd, termios.TIOCSWINSZ, full)
            os.kill(self.proc.pid, signal.SIGWINCH)
        except (ProcessLookupError, OSError):
            pass
        self._wait_for_prompt(ready_timeout)

    def stop(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                os.killpg(os.getpgid(self.proc.pid), signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    self.proc.kill()
                self.proc.wait(timeout=2)
        if self.master_fd is not None:
            try:
                os.close(self.master_fd)
            except OSError:
                pass
            self.master_fd = None

    # ── messaging ────────────────────────────────────────────────────────
    def send_input(self, text: str, submit: bool = True) -> None:
        """Write `text` to claude's PTY stdin. By default, append `\\r` to
        submit (claude TUI expects CR for Enter). Set submit=False to type
        without submitting (e.g. multi-step input)."""
        if not self.proc or self.proc.poll() is not None or self.master_fd is None:
            raise ClaudeNotReady('claude process is not running; call start() first')
        os.write(self.master_fd, text.encode('utf-8'))
        if submit:
            # Brief pause so claude's TUI reflects the typed text before submit.
            time.sleep(0.2)
            os.write(self.master_fd, b'\r')

    # Lines that are TUI chrome / noise (not real assistant content).
    _NOISE_RE = re.compile(
        r'^(?:'
        r'─{4,}'                                    # horizontal divider
        r'|⏵⏵.*bypass permissions'                  # status bar
        r'|.*\([^)]*tokens[^)]*thought for[^)]*\)'  # thinking footer
        r'|.*esc to interrupt'                      # streaming indicator
        r'|.*\d+/\d+\(esc\)'                         # interrupt hint variant
        r'|.*\(ctrl\+o to expand\)'                  # collapsed tool-output hint
        r'|❯?\s*Press up to edit queued messages'    # queued-message hint
        r'|⎿\s+Running…(?:\s*\([^)]*\))?'             # tool-call running spinner: ⎿ Running…  /  ⎿ Running… (5s)
        r'|⎿\s+Tip:.*'                                # tool-block tip hint: ⎿ Tip: …
        r'|❯[\s─━]+'                                  # input-prompt cursor + divider: ❯ ─────…
        r'|(?:[·✻✶✽✳✢]\s+|\.{3,}\s*)[A-Za-z\']{1,32}(?:…|\.{3,})(?:\s*\(.*)?'  # spinner: glyph + word + …
        r')\s*$',
        re.IGNORECASE,
    )

    def _is_noise_line(self, line: str) -> bool:
        s = line.strip()
        if not s:
            return True
        # Pure box-drawing / cursor (input box decoration).
        box_chars = '╭╮╰╯│├┤┬┴┼┌┐└┘─━ '
        if all(c in box_chars + '❯' for c in s):
            return True
        return bool(self._NOISE_RE.match(s))

    def read_new_output(self, timeout: float = 0.5) -> list[str]:
        """Read PTY for up to `timeout` seconds; return new lines (those not
        previously yielded since start()). Filters out TUI chrome (status
        bar, thinking footer, input box decoration). Returns [] if nothing
        new in `timeout` seconds.

        Holds back the bottom-most content line while claude is still
        generating, since pyte renders it character-by-character and we'd
        otherwise emit "Hel" → "Hello" → "Hello world" as three updates of
        the same line. Once a newer content line appears below it (or
        claude returns to idle), the held line is flushed.

        Caller decides when to stop reading — there's no completion concept
        here. Common patterns: read until N seconds quiet, until deadline,
        or until cancel signal.
        """
        chunk = self._read_chunk(timeout)
        if chunk is None:
            return []
        screen = self._screen_lines()
        content_indices = [
            i for i, raw in enumerate(screen)
            if raw.strip() and not self._is_noise_line(raw)
        ]
        # Bottom content line may still be mid-write — hold it until either
        # a newer line is rendered below it, or claude returns to idle.
        held_index = (
            content_indices[-1]
            if content_indices and not self.is_idle()
            else None
        )
        new_lines: list[str] = []
        for i in content_indices:
            if i == held_index:
                continue
            raw = screen[i]
            h = hash(raw.strip())
            if h in self._yielded_line_hashes:
                continue
            self._yielded_line_hashes.add(h)
            new_lines.append(raw.rstrip())
        return new_lines

    def cancel(self) -> None:
        """Interrupt the currently-running generation (Esc, then Ctrl-C as fallback)."""
        if self.master_fd is None:
            return
        # Esc is the Claude Code in-TUI cancel.
        os.write(self.master_fd, b'\x1b')
        time.sleep(0.1)
        # Ctrl-C in case we're not in a state Esc cancels.
        os.write(self.master_fd, b'\x03')

    # ── internal: read loop ──────────────────────────────────────────────
    def _read_chunk(self, timeout: float) -> str | None:
        ready, _, _ = select.select([self.master_fd], [], [], timeout)
        if not ready:
            return None
        try:
            data = os.read(self.master_fd, 8192)
        except OSError:
            return None
        if not data:
            return None
        text = data.decode('utf-8', errors='replace')
        self._buffer_raw += text
        self._buffer_clean = strip_ansi(self._buffer_raw)
        self.last_pty_bytes = time.time()
        # Feed bytes into the virtual terminal so it tracks current screen.
        self._stream.feed(data)
        return text

    def _screen_lines(self) -> list[str]:
        """Return the current rendered screen as a list of lines (rstripped)."""
        return [line.rstrip() for line in self._screen.display]

    def _screen_text(self) -> str:
        """Current screen as a single string with newlines."""
        return '\n'.join(self._screen_lines())

    def _wait_for_prompt(self, timeout: float) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            chunk = self._read_chunk(0.5)
            if chunk is None:
                if self.proc and self.proc.poll() is not None:
                    raise ClaudeNotReady(
                        f'claude exited rc={self.proc.returncode} during startup. '
                        f'Buffer: {self._buffer_clean[-500:]!r}'
                    )
                continue
            if self._looks_like_prompt(self._buffer_clean):
                return
        raise ClaudeNotReady(
            f'No prompt indicator within {timeout}s. '
            f'Last 800 chars: {self._buffer_clean[-800:]!r}'
        )

    def is_idle(self) -> bool:
        """True if claude is back at the prompt and ready for the next message."""
        return self._looks_like_prompt(self._buffer_clean)

    # ── startup readiness ───────────────────────────────────────────────
    # Used only by _wait_for_prompt during start() to decide claude is
    # ready to accept input. Response completion no longer uses this —
    # the streaming API doesn't have a completion concept.
    def _looks_like_prompt(self, clean: str) -> bool:
        """True if claude is idle and waiting for the next user message.

        Tricky: the buffer accumulates every screen frame, so 'esc to
        interrupt' from a *previous* generation is still present long after
        that generation ended. We look for the most-recent occurrence of
        '❯' and check that 'esc to interrupt' is *between* it and the end
        only — i.e. doesn't appear after the current cursor position.
        """
        # Need at least the start of the main TUI.
        squashed = clean.replace(' ', '')
        if 'bypasspermissions' not in squashed:
            return False
        # Find the LAST '❯' and look only at content from there onward.
        last_chevron = clean.rfind('❯')
        if last_chevron == -1:
            return False
        # If 'esc to interrupt' (in any spacing) appears after the latest
        # chevron, claude is still generating. (When idle the chevron is
        # the cursor and is followed only by status-bar redraws without
        # the interrupt hint.)
        after = clean[last_chevron:].replace(' ', '')
        return 'esctointerrupt' not in after


