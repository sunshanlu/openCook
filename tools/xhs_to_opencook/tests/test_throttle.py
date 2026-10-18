from datetime import datetime
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from xhs_to_opencook.chat import ChatSession
from xhs_to_opencook.errors import TaskError
from xhs_to_opencook.storage import Store
from xhs_to_opencook.throttle import HOUR, AccessThrottle

DAY = 86400
T0 = datetime(2023, 11, 15, 12, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()


def make(tmp_path, clock, **options):
    store = Store(tmp_path)
    options.setdefault("access_gap", (30, 30))
    options.setdefault("rng", lambda low, high: low)
    gate = AccessThrottle(
        store,
        "account",
        clock=lambda: clock[0],
        sleeper=lambda seconds: clock.__setitem__(0, clock[0] + seconds),
        **options,
    )
    return store, gate


def full_speed(gate, clock):
    clock[0] += 4 * DAY  # Past the three-day ramp.
    value = gate.state()
    value["ramp_from"] = clock[0] - 4 * DAY
    with gate.store.db:
        gate.save(value)


def test_access_gap_and_hourly_window_with_ramp(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, access_per_hour=4)
    # Ramp halves 4/hour to 2/hour; the gap is also stretched 1/0.5 = 60s.
    gate.access()
    gate.access()
    assert clock[0] == T0 + 60
    gate.access()  # Third visit waits until the first leaves the hour window.
    assert clock[0] == T0 + HOUR
    store.close()


def test_full_speed_after_ramp(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, access_per_hour=4)
    full_speed(gate, clock)
    start = clock[0]
    for _ in range(4):
        gate.access()
    assert clock[0] == start + 90  # Three 30-second gaps, no window wait.
    gate.access()
    assert clock[0] >= start + HOUR
    store.close()


def test_sends_capped_per_hour_and_idempotent(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, send_per_hour=2, send_interval=15)
    full_speed(gate, clock)
    chat = ChatSession(store, None, pacing=gate)
    first = clock[0]
    for index in range(2):
        gate.ready("send")
        chat.persist({"request_id": str(index), "status": "sending", "sent_at": clock[0]})
    chat.persist({"request_id": "1", "status": "sending", "sent_at": clock[0]})  # replay
    count = store.db.execute("select count(*) from throttle_events where kind='send'").fetchone()
    assert count[0] == 2
    gate.ready("send")
    assert clock[0] >= first + HOUR
    store.close()


def test_exhausted_legacy_active_budget_does_not_stop_daytime_work(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, active_hours=1, access_per_hour=10_000, access_gap=(60, 60))
    full_speed(gate, clock)
    start = clock[0]
    value = gate.state()
    value.update(day="2023-11-19", day_active=21600)
    with store.db:
        gate.save(value)
    gate.access()
    assert clock[0] == start
    assert gate.state()["day_active"] == 21600
    store.close()


def test_freeze_backoff_doubles_caps_and_resets(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, backoff_base=7200, backoff_cap=20000, send_interval=1)
    full_speed(gate, clock)
    chat = ChatSession(store, None, pacing=gate)
    assert gate.freeze("platform_restricted") == clock[0] + 7200
    assert gate.freeze("platform_restricted") == clock[0] + 14400
    assert gate.freeze("platform_restricted") == clock[0] + 20000  # capped
    with pytest.raises(TaskError) as failure:
        gate.ready("send")
    assert failure.value.code == "runtime_wait" or "冻结" in str(failure.value)
    clock[0] += 20001
    for index in range(20):
        clock[0] += 1
        gate.ready("send")
        chat.persist({"request_id": f"s{index}", "status": "sending", "sent_at": clock[0]})
        clock[0] += 200  # Stay inside the hourly send window budget.
    assert gate.state()["freeze_count"] == 0
    assert gate.freeze("platform_restricted") == clock[0] + 7200
    store.close()


def test_freeze_blocks_reserve_and_release_keeps_ladder(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock)
    full_speed(gate, clock)
    gate.freeze("platform_restricted")
    with pytest.raises(TaskError) as failure:
        gate.reserve({"request_id": "r", "sent_at": clock[0]})
    assert failure.value.code == "pacing_frozen"
    gate.release()
    assert gate.state()["frozen_until"] == 0
    assert gate.freeze("platform_restricted") == clock[0] + 14400  # ladder survived release
    store.close()


def test_state_survives_restart(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, access_per_hour=2)
    gate.access()
    store.close()
    store, gate = make(tmp_path, clock, access_per_hour=2)
    assert len(gate.used("access", clock[0])) == 1
    store.close()


def test_sends_are_spread_across_the_hour_not_burst(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, send_per_hour=4, send_interval=15)
    full_speed(gate, clock)
    chat = ChatSession(store, None, pacing=gate)
    times = []
    for index in range(4):
        gate.ready("send")
        chat.persist({"request_id": str(index), "status": "sending", "sent_at": clock[0]})
        times.append(clock[0])
    gaps = [b - a for a, b in zip(times, times[1:], strict=False)]
    assert all(gap >= HOUR / 4 * 0.8 for gap in gaps)  # 900s average, 20% jitter floor
    assert times[-1] - times[0] < HOUR  # The whole allowance still fits inside the hour.
    store.close()


def test_spacing_never_drops_below_the_send_interval_and_follows_the_ramp(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, send_per_hour=7200, send_interval=15)
    full_speed(gate, clock)
    chat = ChatSession(store, None, pacing=gate)
    gate.ready("send")
    chat.persist({"request_id": "a", "status": "sending", "sent_at": clock[0]})
    start = clock[0]
    gate.ready("send")
    assert clock[0] - start == 15  # 3600/7200 = 0.5s would be shorter than the floor
    store.close()
    clock = [T0]
    store, gate = make(tmp_path / "ramp", clock, send_per_hour=40, send_interval=15)
    chat = ChatSession(store, None, pacing=gate)
    gate.ready("send")
    chat.persist({"request_id": "a", "status": "sending", "sent_at": clock[0]})
    start = clock[0]
    gate.ready("send")
    assert clock[0] - start >= HOUR / 20 * 0.8  # Ramp halves 40/h to 20/h: 180s spacing.
    store.close()


def test_spacing_survives_restart(tmp_path):
    clock = [T0]
    store, gate = make(tmp_path, clock, send_per_hour=4)
    full_speed(gate, clock)
    chat = ChatSession(store, None, pacing=gate)
    gate.ready("send")
    chat.persist({"request_id": "a", "status": "sending", "sent_at": clock[0]})
    start = clock[0]
    store.close()
    store, gate = make(tmp_path, clock, send_per_hour=4)
    gate.ready("send")
    assert clock[0] - start >= HOUR / 4 * 0.8
    store.close()


@pytest.mark.parametrize("hour,minute,expected_hour,expected_minute", [
    (6, 59, 7, 0), (7, 0, 7, 0), (23, 59, 23, 59), (0, 0, 7, 0),
])
def test_daily_window_uses_beijing_clock(tmp_path, monkeypatch, hour, minute,
                                        expected_hour, expected_minute):
    monkeypatch.setenv("TZ", "UTC")
    zone = ZoneInfo("Asia/Shanghai")
    clock = [datetime(2026, 10, 18, hour, minute, tzinfo=zone).timestamp()]
    store, gate = make(tmp_path, clock, ramp_days=0)
    gate.ready()
    assert clock[0] == datetime(2026, 10, 18, expected_hour, expected_minute,
                               tzinfo=zone).timestamp()
    store.close()


def test_night_recovery_allows_visits_but_never_new_sends(tmp_path):
    start = datetime(2026, 10, 18, 0, 0, tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()
    clock = [start]
    store, gate = make(tmp_path, clock, ramp_days=0)
    with gate.recover_submission():
        gate.access()
        assert clock[0] == start
        with pytest.raises(TaskError) as failure:
            gate.reserve({"request_id": "new", "sent_at": start})
        assert failure.value.code == "pacing_window_closed"
    gate.ready()
    assert clock[0] == start + 7 * HOUR
    store.close()


def test_midnight_during_preparation_keeps_request_unsent_and_attempt_intact(tmp_path):
    clock = [datetime(2026, 10, 18, 23, 59, 59,
                      tzinfo=ZoneInfo("Asia/Shanghai")).timestamp()]
    store, gate = make(tmp_path, clock, ramp_days=0)

    def prepare(prompt, source, request, persist):
        clock[0] += 1
        request.update(status="sending", sent_at=clock[0])
        persist(request)
        pytest.fail("The reservation must reject before Enter")

    chat = ChatSession(store, lambda: SimpleNamespace(begin_request=prepare), pacing=gate)
    request = {"request_id": "boundary", "status": "prepared", "attempt": 1}
    chat.persist(request)
    with pytest.raises(TaskError) as failure:
        chat.begin("prompt", None, request)
    assert failure.value.code == "pacing_window_closed"
    saved = store.stage_get("chat_requests", "boundary")
    assert saved["status"] == "prepared" and saved["attempt"] == 1
    assert "sent_at" not in saved and gate.used("send", clock[0]) == []
    store.close()
