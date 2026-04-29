"""
ClaudeCode — long-lived `claude` interactive process under a PTY.

Wraps the `claude` CLI in interactive (TUI) mode so we can reuse the same
code path that `claude --print` is built on top of, but without --print's
known streaming-response hangs on tool_use.

Usage:
    cc = ClaudeCode(cwd="/path/to/project", model="claude-sonnet-4-6")
    cc.start()
    reply = cc.send("run ls in current dir")
    print(reply)
    cc.stop()

This is intentionally a thin wrapper. Response-completion detection is
heuristic (read until the input prompt redraws or the stream goes idle
past a stall threshold).
"""

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
    ):
        self.cwd = cwd
        self.model = model
        self.session_id = session_id
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

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self, ready_timeout: float = 30.0) -> None:
        cmd = [self.claude_bin, '--dangerously-skip-permissions']
        if self.model:
            cmd += ['--model', self.model]
        if self.session_id:
            cmd += ['resume', self.session_id]
        if self.debug_log:
            cmd += ['--debug-file', self.debug_log]

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
    def send(
        self,
        prompt: str,
        response_timeout: float = 600.0,
        stall_timeout: float = 90.0,
    ) -> str:
        if not self.proc or self.proc.poll() is not None or self.master_fd is None:
            raise ClaudeNotReady('claude process is not running; call start() first')
        # Clear buffers for this turn.
        self._buffer_raw = ''
        self._buffer_clean = ''
        # Type the prompt. For multi-line, claude treats raw \n as newline
        # within the input field, not submit. Submit is a final \r after
        # the input is settled.
        body = prompt.replace('\n', '\\\n')  # unused, leave plain newlines
        os.write(self.master_fd, prompt.encode('utf-8'))
        # Brief pause so claude's TUI reflects the typed text before submit.
        time.sleep(0.2)
        # Submit with Enter. Use \r (CR) — what TTYs normally send for Enter.
        os.write(self.master_fd, b'\r')
        self._read_until_idle(response_timeout, stall_timeout)
        return self._extract_response(prompt)

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

    def _read_until_idle(self, response_timeout: float, stall_timeout: float) -> str:
        """Read until claude redraws its input prompt (response complete) OR
        the stream is silent for stall_timeout (probable hang)."""
        deadline = time.time() + response_timeout
        last_activity = time.time()
        # Track buffer length at last "prompt visible" check so we don't
        # detect stale prompt artifacts from before the user's submission.
        seen_post_submit_output = False
        while time.time() < deadline:
            chunk = self._read_chunk(0.5)
            now = time.time()
            if chunk:
                last_activity = now
                seen_post_submit_output = True
                # Prompt redraw is the completion signal — but only if we've
                # already seen at least some output since submit.
                if seen_post_submit_output and self._looks_like_prompt(self._buffer_clean):
                    # Heuristic: only call it done if last few non-empty lines
                    # contain typical "ready for next message" markers.
                    if self._response_complete():
                        return self._buffer_clean
            else:
                if now - last_activity > stall_timeout:
                    raise ClaudeStalled(
                        f'No output for {stall_timeout}s mid-response. '
                        f'Last 400: {self._buffer_clean[-400:]!r}'
                    )
                if self.proc and self.proc.poll() is not None:
                    raise ClaudeNotReady(
                        f'claude exited rc={self.proc.returncode} during response.'
                    )
        raise TimeoutError(f'Response not complete within {response_timeout}s.')

    # ── heuristic detectors ──────────────────────────────────────────────
    # These will need tuning once we see actual claude TUI output. Keep
    # the matchers conservative: false-positive completion is the worst
    # failure mode (we'd cut off mid-response).

    # Markers observed in the actual claude TUI (v2.1.123) AFTER ANSI strip:
    #   '❯' is the input cursor (visible when idle, waiting for input)
    #   'esc to interrupt' appears only during generation. Note that since
    #     box-drawing compacts text horizontally, words may run together
    #     ('esctointerrupt'), so check both forms.
    #   'bypass permissions' (or 'bypasspermissions') is in the status bar
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

    def _response_complete(self) -> bool:
        return self._looks_like_prompt(self._buffer_clean)

    # ── response extraction ──────────────────────────────────────────────
    def _extract_response(self, sent_prompt: str) -> str:
        """Pull the assistant's reply text from the rendered screen.

        Layout we observed in claude TUI v2.1.123:

            (top blank lines)
            ❯ <user prompt echo>
            ⏺ <assistant response, possibly multi-line>
              ⎿ <tool result, indented under tool-call>     (when tools used)
            ⏺ <more assistant>                              (multi-step)
            ────────────────  (divider)
            ❯                  (input cursor — empty, idle)
            ────────────────  (divider)
            ⏵⏵ bypass permissions on …  (status bar)

        Strategy: find the LAST `❯ <prompt>` echo and take everything between
        it and the empty-input `❯` line near the bottom dividers. Strip
        leading `⏺ ` markers from response lines; keep `⎿` indented blocks.
        """
        lines = self._screen_lines()
        if not lines:
            return ''
        # Locate the input divider near the bottom: a long run of '─'.
        divider_idx = None
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].count('─') > 20:
                divider_idx = i
                break
        # Locate the user-prompt echo: a '❯' line whose content starts with
        # the prompt we just sent (after stripping '❯' and spaces).
        prompt_first_word = sent_prompt.split('\n')[0].strip()[:30]
        echo_idx = None
        for i in range(len(lines) - 1, -1, -1):
            line = lines[i]
            if '❯' in line and prompt_first_word and prompt_first_word[:15] in line:
                echo_idx = i
                break
        if echo_idx is None:
            # Fallback: everything above the bottom divider.
            top = 0
            bottom = divider_idx if divider_idx is not None else len(lines)
            return self._clean_response_block(lines[top:bottom])
        top = echo_idx + 1
        bottom = divider_idx if divider_idx is not None else len(lines)
        return self._clean_response_block(lines[top:bottom])

    # Lines that are TUI chrome rather than response content.
    _CHROME_RE = re.compile(
        r'^\s*('
        r'❯\s*'                                    # empty input cursor
        r'|─{20,}.*'                               # horizontal divider
        r'|[✢✳✶✻✽●○]\s*\w+ for \d+\w*\s*$'        # "✻ Baked for 1s" etc.
        r'|[✢✳✶✻✽✻●○]\s*(Generating|Percolating|Crafting|Cooking).*'
        r'|⏵⏵.*bypass permissions.*'               # status bar
        r')\s*$'
    )

    @classmethod
    def _clean_response_block(cls, lines: list[str]) -> str:
        out: list[str] = []
        for raw in lines:
            s = raw.rstrip()
            if not s:
                if out and out[-1] != '':
                    out.append('')
                continue
            if cls._CHROME_RE.match(s):
                continue
            # Strip leading '⏺ ' / '⏺' marking an assistant message.
            if s.lstrip().startswith('⏺'):
                s = s.lstrip()[1:].lstrip()
            out.append(s)
        # Trim leading/trailing blanks.
        while out and out[0] == '':
            out.pop(0)
        while out and out[-1] == '':
            out.pop()
        return '\n'.join(out)
