"""
ClaudeJsonStream — thin wrapper around one `claude --print --output-format stream-json` invocation.

Each instance represents a single prompt→response round. Call start() to
spawn the subprocess, then iterate over iter_events() to consume the NDJSON
event stream. stop() kills the process if it is still running.

Usage:
    cjs = ClaudeJsonStream(
        prompt="what is 2+2?",
        cwd="/tmp",
        model="claude-sonnet-4-6",
    )
    cjs.start()
    for event in cjs.iter_events():
        print(event)  # {"type": "assistant", ...} / {"type": "result", ...}
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import time
from typing import Iterator, Optional


class ClaudeJsonStream:
    """One `claude --print --output-format stream-json` subprocess."""

    def __init__(
        self,
        prompt: str,
        cwd: str | None = None,
        model: str | None = None,
        session_id: str | None = None,
        claude_bin: str | None = None,
        debug_log: str | None = None,
        extra_env: dict | None = None,
    ):
        self.prompt = prompt
        self.cwd = cwd
        self.model = model
        self.session_id = session_id
        self.extra_env = dict(extra_env) if extra_env else {}
        self.claude_bin = (
            claude_bin
            or shutil.which('claude')
            or os.path.expanduser('~/.local/bin/claude')
        )
        if debug_log is None:
            debug_dir = os.path.expanduser('~/.cta/debug')
            os.makedirs(debug_dir, exist_ok=True)
            debug_log = os.path.join(debug_dir, f'cjs-{int(time.time())}.log')
        self.debug_log = debug_log
        self.proc: Optional[subprocess.Popen] = None

    def _build_cmd(self) -> list[str]:
        cmd = [
            self.claude_bin,
            '--print', '--dangerously-skip-permissions',
            '--output-format', 'stream-json',
            '--include-partial-messages',
            '--verbose',
            '--debug-file', self.debug_log,
        ]
        if self.model:
            cmd += ['--model', self.model]
        if self.session_id:
            cmd += ['--resume', self.session_id]
        cmd += ['-p', self.prompt]
        return cmd

    def start(self) -> None:
        """Spawn the subprocess. Raises FileNotFoundError if claude is not found."""
        env = os.environ.copy()
        env.update(self.extra_env)
        self.proc = subprocess.Popen(
            self._build_cmd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,  # stderr goes to --debug-file; piping risks deadlock
            stdin=subprocess.DEVNULL,
            text=True,
            cwd=self.cwd,
            env=env,
            start_new_session=True,
        )

    def iter_events(self) -> Iterator[dict]:
        """Yield parsed JSON event dicts from stdout until EOF.

        Each line is one NDJSON object. Non-JSON lines are silently skipped.
        Caller must have called start() first. The subprocess is waited on
        after the last line is consumed.
        """
        if self.proc is None:
            raise RuntimeError('call start() before iter_events()')
        for raw in iter(self.proc.stdout.readline, ''):
            line = raw.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue
        self.proc.wait()

    def stop(self) -> None:
        """Kill the subprocess if it is still running. Idempotent."""
        if self.proc is None or self.proc.poll() is not None:
            return
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
            self.proc.wait()
