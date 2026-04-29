"""
Manual driver for ClaudeCode against a real claude binary.

Run directly:
    python3 test_claude_code_real.py [scenario]

Scenarios:
    basic     simple prompt, no tool use (default)
    bash      "run ls" — exercises the case --print hangs on
    multi     two turns in the same session
    cancel    start a long task, cancel mid-flight

Not part of the unit test suite (test_*.py). This drives the actual claude
CLI under a PTY so we can observe how the TUI behaves and iterate on the
prompt-completion heuristic.
"""

import sys
import textwrap
import time

from claude_code import ClaudeCode, ClaudeStalled, ClaudeNotReady, strip_ansi


def show(label: str, text: str, head: int = 600, tail: int = 600):
    print(f'\n=== {label} (len={len(text)}) ===')
    if len(text) <= head + tail:
        print(text)
    else:
        print(text[:head])
        print(f'\n... [{len(text) - head - tail} chars elided] ...\n')
        print(text[-tail:])


def scenario_basic(cwd: str):
    cc = ClaudeCode(cwd=cwd, model='claude-sonnet-4-6')
    print(f'[basic] starting claude in {cwd}…')
    t0 = time.time()
    cc.start(ready_timeout=30)
    print(f'[basic] ready in {time.time() - t0:.1f}s')
    print(f'[basic] sending: "just say hi"')
    t0 = time.time()
    try:
        reply = cc.send('just say hi', response_timeout=60, stall_timeout=30)
        print(f'[basic] got reply in {time.time() - t0:.1f}s')
        show('reply', reply, head=1500, tail=1500)
    finally:
        cc.stop()


def scenario_bash(cwd: str):
    cc = ClaudeCode(cwd=cwd, model='claude-sonnet-4-6')
    print(f'[bash] starting claude in {cwd}…')
    cc.start(ready_timeout=30)
    print(f'[bash] sending: "run ls in current dir"')
    t0 = time.time()
    try:
        reply = cc.send('run ls in current dir', response_timeout=120, stall_timeout=60)
        print(f'[bash] got reply in {time.time() - t0:.1f}s')
        show('reply', reply, head=2000, tail=2000)
    except ClaudeStalled as e:
        print(f'[bash] STALLED: {e}')
    finally:
        cc.stop()


def scenario_multi(cwd: str):
    cc = ClaudeCode(cwd=cwd, model='claude-sonnet-4-6')
    cc.start(ready_timeout=30)
    try:
        for i, msg in enumerate(['my favourite number is 7', 'what number did i tell you?']):
            print(f'[multi] turn {i + 1}: {msg!r}')
            t0 = time.time()
            reply = cc.send(msg, response_timeout=60, stall_timeout=30)
            print(f'[multi] turn {i + 1} done in {time.time() - t0:.1f}s')
            show(f'turn {i + 1}', reply, head=600, tail=600)
    finally:
        cc.stop()


def scenario_cancel(cwd: str):
    cc = ClaudeCode(cwd=cwd, model='claude-sonnet-4-6')
    cc.start(ready_timeout=30)
    print(f'[cancel] sending a long prompt and cancelling after 3s…')
    try:
        # Don't await reply; we'll cancel.
        import threading
        stalled = []
        def runner():
            try:
                cc.send('count from 1 to 1000 slowly, one number per line', response_timeout=60, stall_timeout=30)
            except Exception as e:
                stalled.append(e)
        t = threading.Thread(target=runner, daemon=True)
        t.start()
        time.sleep(3)
        cc.cancel()
        t.join(timeout=10)
        print(f'[cancel] runner returned, stalled={stalled}')
    finally:
        cc.stop()


def scenario_model(cwd: str):
    """Exercise: send message → /model (read current) → /model <other> (switch) → send again.

    Validates that the PTY wrapper can drive slash commands and that conversation
    continues across a model switch. We start with sonnet-4-6 and switch to opus-4-6.
    """
    cc = ClaudeCode(cwd=cwd, model='claude-sonnet-4-6')
    print(f'[model] starting claude (sonnet) in {cwd}…')
    cc.start(ready_timeout=30)
    try:
        # Turn 1: send a message on the starting model.
        print(f'[model] turn 1: send "what is 2+2"')
        t0 = time.time()
        reply1 = cc.send('what is 2+2 — answer with just the number', response_timeout=60, stall_timeout=30)
        print(f'[model] turn 1 done in {time.time() - t0:.1f}s')
        show('turn1 reply', reply1, head=400, tail=600)

        # /model with no arg → claude shows current model in a TUI selector.
        # We just send the command, wait for it to settle, then dismiss.
        print(f'[model] querying current model with /model')
        t0 = time.time()
        try:
            reply_show = cc.send('/model', response_timeout=15, stall_timeout=10)
        except Exception as e:
            print(f'[model] /model bare raised {type(e).__name__}: {e}')
            reply_show = '(no completion — likely a popup)'
            # Send Esc to dismiss any popup
            import os
            os.write(cc.master_fd, b'\x1b')
            time.sleep(0.5)
        show('/model output', reply_show, head=600, tail=600)

        # /model <name> → switch to a different model directly.
        new_model = 'claude-opus-4-6'
        print(f'[model] switching to {new_model} via /model {new_model}')
        t0 = time.time()
        try:
            reply_switch = cc.send(f'/model {new_model}', response_timeout=15, stall_timeout=10)
        except Exception as e:
            print(f'[model] /model <name> raised {type(e).__name__}: {e}')
            reply_switch = ''
        show('/model switch output', reply_switch, head=400, tail=600)

        # Turn 2: send another message — should now run on the new model.
        # Expect the status bar at the bottom to reflect the new model name.
        print(f'[model] turn 2: send "what is 5*7"')
        t0 = time.time()
        reply2 = cc.send('what is 5*7 — just the number', response_timeout=60, stall_timeout=30)
        print(f'[model] turn 2 done in {time.time() - t0:.1f}s')
        show('turn2 reply', reply2, head=400, tail=600)

        # Sanity-check: the TUI's header line shows model name. After the
        # switch we expect to see "opus-4-6" somewhere in the most recent
        # frame. Crude check on the buffer.
        squashed = reply2.replace(' ', '').lower()
        if 'opus-4-6' in squashed or 'opus' in squashed:
            print(f'[model] ✓ buffer shows opus → switch took effect')
        else:
            print(f'[model] ⚠ no "opus" marker in turn2 reply — switch may not have applied')
    finally:
        cc.stop()


SCENARIOS = {
    'basic': scenario_basic,
    'bash': scenario_bash,
    'multi': scenario_multi,
    'cancel': scenario_cancel,
    'model': scenario_model,
}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else 'basic'
    cwd = sys.argv[2] if len(sys.argv) > 2 else '/Users/alex/projects/cta'
    if name not in SCENARIOS:
        print(f'unknown scenario {name!r}; choices: {list(SCENARIOS)}')
        sys.exit(2)
    SCENARIOS[name](cwd)


if __name__ == '__main__':
    main()
