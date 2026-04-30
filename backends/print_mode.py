"""PrintBackend — wraps `claude --print` (subprocess-per-prompt).

Synchronous: `send` blocks until the subprocess returns, then invokes
``on_output`` once with the full reply.
"""

from __future__ import annotations

import os
import signal

from .base import ClaudeBackend


class PrintBackend(ClaudeBackend):
    def send(self, prompt: str) -> None:
        import agent  # late import; agent ↔ backends are mutually referencing

        key = self.key
        cwd = agent.user_cwd.get(key, agent.DEFAULT_CWD)
        model = agent.user_model.get(key, agent.MODEL)
        timeout = agent.user_timeout.get(key, agent.TIMEOUT)
        session_id = agent.user_sessions.get(key)

        agent.claude_active_keys.add(key)
        try:
            reply, new_sid = agent.call_claude(
                prompt, cwd=cwd, session_id=session_id, model=model,
                timeout=timeout, uid=self.uid, chat_id=self.chat_id,
            )
            if session_id and "No conversation found with session ID" in reply:
                agent.user_sessions.pop(key, None)
                reply, new_sid = agent.call_claude(
                    prompt, cwd=cwd, session_id=None, model=model,
                    timeout=timeout, uid=self.uid, chat_id=self.chat_id,
                )
        finally:
            agent.claude_active_keys.discard(key)

        # Honour /cancel: drop the reply and consume the flag so the next prompt
        # is not also suppressed.
        if key in agent._cancelled_keys:
            agent._cancelled_keys.discard(key)
            return
        if new_sid:
            agent.user_sessions[key] = new_sid
        if self.on_output is not None:
            self.on_output(reply)

    def cancel(self) -> bool:
        import agent

        key = self.key
        proc = agent._current_procs.get(key)
        if proc is not None:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                try:
                    proc.kill()
                except Exception:
                    return False
            agent._cancelled_keys.add(key)
            return True
        # In-flight but no subprocess yet — likely blocked at the semaphore.
        # Mark cancelled so call_claude bails out as soon as a slot opens.
        if key in agent.claude_active_keys:
            agent._cancelled_keys.add(key)
            return True
        return False

    def stop(self) -> None:
        # Print mode owns no persistent resources; nothing to release.
        return None
