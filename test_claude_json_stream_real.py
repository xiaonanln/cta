"""
Manual integration test for ClaudeJsonStream against the real claude binary.

Run directly:
    python3 test_claude_json_stream_real.py [scenario]

Scenarios:
    basic     simple prompt, collect all events, verify text + result
    resume    two turns using session_id from the first
    cancel    start a prompt, stop() mid-stream, verify process dies

Not part of the unit test suite — this drives the actual claude CLI.
"""

from __future__ import annotations

import sys
import time

from claude_json_stream import ClaudeJsonStream


CWD = '/Users/alex/projects/cta'
MODEL = 'claude-sonnet-4-6'


def show(label: str, items: list, limit: int = 20):
    print(f'\n=== {label} ({len(items)} item(s)) ===')
    for item in items[:limit]:
        print(f'  {item}')
    if len(items) > limit:
        print(f'  … {len(items) - limit} more')


def collect(cjs: ClaudeJsonStream) -> tuple[list[str], str]:
    """Drain all events; return (text_deltas, session_id)."""
    deltas: list[str] = []
    session_id = ''

    for event in cjs.iter_events():
        etype = event.get('type')
        if etype == 'stream_event':
            inner = event.get('event') or {}
            if inner.get('type') == 'content_block_delta':
                delta = (inner.get('delta') or {})
                if delta.get('type') == 'text_delta':
                    text = delta.get('text', '')
                    if text:
                        deltas.append(text)
        elif etype == 'result':
            session_id = event.get('session_id', '')
            if event.get('is_error') or event.get('subtype') == 'error':
                print(f'  [result] ERROR: {event.get("result")}')
            else:
                print(f'  [result] ok session_id={session_id!r} '
                      f'cost_usd={event.get("cost_usd")}')

    return deltas, session_id


def scenario_basic():
    """Send a math question; verify text events arrive and result has a session_id."""
    prompt = 'what is 2+2? reply with just the digit and nothing else.'
    print(f'[basic] prompt={prompt!r}')
    cjs = ClaudeJsonStream(prompt=prompt, cwd=CWD, model=MODEL)
    t0 = time.time()
    cjs.start()
    deltas, session_id = collect(cjs)
    elapsed = time.time() - t0

    full_reply = ''.join(deltas).strip()
    print(f'[basic] reply={full_reply!r} elapsed={elapsed:.1f}s')
    show('text deltas', deltas)

    assert deltas, 'FAIL: no text deltas received'
    assert '4' in full_reply, f'FAIL: "4" not in reply {full_reply!r}'
    assert session_id, 'FAIL: no session_id in result event'
    print('[basic] ✅ PASS')
    return session_id


def scenario_resume(session_id: str):
    """Use the session_id from the previous turn to ask a follow-up."""
    prompt = 'multiply that answer by 10. reply with just the number.'
    print(f'\n[resume] session_id={session_id!r}')
    print(f'[resume] prompt={prompt!r}')
    cjs = ClaudeJsonStream(prompt=prompt, cwd=CWD, model=MODEL, session_id=session_id)
    t0 = time.time()
    cjs.start()
    deltas, new_sid = collect(cjs)
    elapsed = time.time() - t0

    full_reply = ''.join(deltas).strip()
    print(f'[resume] reply={full_reply!r} elapsed={elapsed:.1f}s')

    assert deltas, 'FAIL: no text deltas received'
    assert '40' in full_reply, f'FAIL: "40" not in reply {full_reply!r}'
    assert new_sid, 'FAIL: no session_id in result event'
    print('[resume] ✅ PASS')


def scenario_cancel():
    """Start a long-running prompt and stop() it before it finishes."""
    prompt = 'count from 1 to 500, one number per line, no other text.'
    print(f'\n[cancel] prompt={prompt!r}')
    cjs = ClaudeJsonStream(prompt=prompt, cwd=CWD, model=MODEL)
    cjs.start()

    events_seen = 0
    stopped_early = False
    t0 = time.time()
    for event in cjs.iter_events():
        events_seen += 1
        if event.get('type') == 'assistant':
            # Kill after receiving the first assistant event (mid-stream).
            if events_seen >= 1:
                print(f'  [cancel] stopping after {events_seen} event(s)…')
                cjs.stop()
                stopped_early = True
                break

    elapsed = time.time() - t0
    rc = cjs.proc.returncode if cjs.proc else None
    print(f'[cancel] stopped_early={stopped_early} rc={rc} elapsed={elapsed:.1f}s')

    assert stopped_early, 'FAIL: loop never broke early'
    assert cjs.proc is not None and cjs.proc.poll() is not None, \
        'FAIL: process is still running after stop()'
    print('[cancel] ✅ PASS')


def scenario_invalid_session():
    """Pass a bogus session_id; verify the result event has the signature that
    JsonStreamBackend uses to detect a stale session and trigger a retry:
    is_error=True, num_turns=0, empty result string.

    Note: claude writes "No conversation found with session ID" to stderr, which
    we devnull. The result event carries no human-readable message, so detection
    is purely structural.
    """
    bogus_sid = '00000000-0000-0000-0000-000000000000'
    prompt = 'what is 2+2? reply with just the digit.'
    print(f'\n[invalid_session] session_id={bogus_sid!r}')
    print(f'[invalid_session] prompt={prompt!r}')

    cjs = ClaudeJsonStream(prompt=prompt, cwd=CWD, model=MODEL, session_id=bogus_sid)
    cjs.start()

    result_event = None
    for event in cjs.iter_events():
        if event.get('type') == 'result':
            result_event = event
            print(f'  [result] is_error={event.get("is_error")} '
                  f'num_turns={event.get("num_turns")} '
                  f'subtype={event.get("subtype")!r} '
                  f'result={event.get("result","")[:80]!r}')

    assert result_event is not None, 'FAIL: no result event received'
    assert result_event.get('is_error'), 'FAIL: expected is_error=True'
    assert result_event.get('num_turns', -1) == 0, \
        f'FAIL: expected num_turns=0, got {result_event.get("num_turns")}'
    assert not (result_event.get('result') or '').strip(), \
        f'FAIL: expected empty result string, got {result_event.get("result")!r}'
    print('[invalid_session] ✅ PASS — result matches retry-trigger signature (is_error=True, num_turns=0, result="")')


SCENARIOS = {
    'basic': lambda: scenario_basic(),
    'resume': lambda: scenario_resume(scenario_basic()),
    'cancel': scenario_cancel,
    'invalid_session': scenario_invalid_session,
    'all': None,
}


def main():
    name = sys.argv[1] if len(sys.argv) > 1 else 'all'
    if name == 'all':
        sid = scenario_basic()
        scenario_resume(sid)
        scenario_cancel()
        scenario_invalid_session()
        print('\n✅ All scenarios passed.')
        return
    if name not in SCENARIOS:
        print(f'unknown scenario {name!r}; choices: {sorted(SCENARIOS)}')
        sys.exit(2)
    SCENARIOS[name]()


if __name__ == '__main__':
    main()
