"""Byte-level filter tests for ZoneDaemon._record_callback.

The CPU optimisation added a pre-parse fast path that drops uninteresting
events by inspecting only their first byte.  The regression was that it also
dropped MotionNotify while in BUTTON1_DOWN, starving the state machine of the
motion it needs to recognise a drag.  These tests pin down *exactly* which
events the filter forwards to `_handle` in each state, without a real X server.

`_record_callback` calls ``rq.EventField(None).parse_binary_value(...)`` to
decode each 32-byte event.  We monkeypatch that parser with a fake that simply
consumes 32 bytes and yields a sentinel, so we can assert purely on whether the
filter *chose* to parse-and-handle an event or skip it.
"""

from types import SimpleNamespace

import Xlib.X as X
import Xlib.ext.record as record
import Xlib.protocol.rq as rq

from daemon import _State


# Core X event type numbers (first byte of each RECORD event record).
KEY_PRESS    = X.KeyPress       # 2
KEY_RELEASE  = X.KeyRelease     # 3
BUTTON_PRESS = X.ButtonPress    # 4
MOTION       = X.MotionNotify   # 6


class _FakeParser:
    """Stands in for rq.EventField: consumes one 32-byte event record."""
    def __init__(self, *_a, **_k):
        pass

    def parse_binary_value(self, data, _display, _fmt, _parent):
        return SimpleNamespace(type=data[0] & 0x7f), data[32:]


def _reply(*event_type_bytes):
    """Build a RECORD reply whose data is a run of 32-byte event records."""
    blob = b"".join(bytes([t]) + bytes(31) for t in event_type_bytes)
    return SimpleNamespace(category=record.FromServer,
                           client_swapped=False, data=blob)


def _run_callback(monkeypatch, daemon, reply):
    """Invoke _record_callback with the parser stubbed; return handled events."""
    monkeypatch.setattr(rq, "EventField", _FakeParser)
    handled = []
    daemon._handle = lambda ev: handled.append(ev.type)
    daemon._record_callback(reply)
    return handled


# --------------------------------------------------- motion forwarding by state

def test_motion_forwarded_in_button1_down(make_daemon, monkeypatch):
    """REGRESSION: motion must reach _handle while in BUTTON1_DOWN."""
    d = make_daemon(state=_State.BUTTON1_DOWN)
    handled = _run_callback(monkeypatch, d, _reply(MOTION))
    assert handled == [MOTION]


def test_motion_forwarded_while_dragging(make_daemon, monkeypatch):
    d = make_daemon(state=_State.DRAGGING)
    assert _run_callback(monkeypatch, d, _reply(MOTION)) == [MOTION]


def test_motion_forwarded_while_overlay_active(make_daemon, monkeypatch):
    d = make_daemon(state=_State.OVERLAY_ACTIVE)
    assert _run_callback(monkeypatch, d, _reply(MOTION)) == [MOTION]


def test_motion_filtered_when_idle(make_daemon, monkeypatch):
    """Idle motion is pure noise and SHOULD be dropped (the CPU win we keep)."""
    d = make_daemon(state=_State.IDLE)
    assert _run_callback(monkeypatch, d, _reply(MOTION)) == []


# --------------------------------------------------- button events always pass

def test_button_press_always_forwarded(make_daemon, monkeypatch):
    d = make_daemon(state=_State.IDLE)
    assert _run_callback(monkeypatch, d, _reply(BUTTON_PRESS)) == [BUTTON_PRESS]


# --------------------------------------------------- keyboard gated by mod_snap

def test_keyboard_filtered_when_modifier_disabled(make_daemon, monkeypatch):
    d = make_daemon(state=_State.DRAGGING, mod_snap=False)
    assert _run_callback(monkeypatch, d, _reply(KEY_PRESS, KEY_RELEASE)) == []


def test_keyboard_forwarded_when_modifier_enabled(make_daemon, monkeypatch):
    d = make_daemon(state=_State.DRAGGING, mod_snap=True)
    handled = _run_callback(monkeypatch, d, _reply(KEY_PRESS, KEY_RELEASE))
    assert handled == [KEY_PRESS, KEY_RELEASE]


# --------------------------------------------------- mixed packet ordering

def test_mixed_packet_forwards_correct_subset_when_idle(make_daemon, monkeypatch):
    """A packet with motion + button at IDLE: drop the motion, keep the button."""
    d = make_daemon(state=_State.IDLE, mod_snap=False)
    handled = _run_callback(monkeypatch, d,
                            _reply(MOTION, BUTTON_PRESS, MOTION))
    assert handled == [BUTTON_PRESS]


def test_mixed_packet_forwards_all_relevant_while_dragging(make_daemon, monkeypatch):
    d = make_daemon(state=_State.DRAGGING, mod_snap=False)
    handled = _run_callback(monkeypatch, d,
                            _reply(MOTION, BUTTON_PRESS, MOTION))
    assert handled == [MOTION, BUTTON_PRESS, MOTION]


# --------------------------------------------------- reply guards

def test_non_server_category_ignored(make_daemon, monkeypatch):
    d = make_daemon(state=_State.DRAGGING)
    reply = SimpleNamespace(category=record.FromClient,
                            client_swapped=False, data=bytes([MOTION]) + bytes(31))
    assert _run_callback(monkeypatch, d, reply) == []


def test_short_trailing_bytes_ignored(make_daemon, monkeypatch):
    """A truncated (<32 byte) trailing record must not raise or be handled."""
    d = make_daemon(state=_State.DRAGGING)
    reply = SimpleNamespace(category=record.FromServer,
                            client_swapped=False,
                            data=bytes([MOTION]) + bytes(10))   # only 11 bytes
    assert _run_callback(monkeypatch, d, reply) == []
