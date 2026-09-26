import subprocess

from src.power import PowerState, SleepGuard, SleepPolicy, network_up, parse_pmset_batt

AC_TEXT = ("Now drawing from 'AC Power'\n"
           " -InternalBattery-0 (id=26607715)\t100%; charged; 0:00 remaining present: true\n")
BATT_TEXT = ("Now drawing from 'Battery Power'\n"
             " -InternalBattery-0 (id=26607715)\t57%; discharging; 6:12 remaining present: true\n")
DESKTOP_TEXT = "Now drawing from 'AC Power'\n"


def test_parse_pmset_batt():
    assert parse_pmset_batt(AC_TEXT) == PowerState(on_ac=True, battery_pct=100)
    assert parse_pmset_batt(BATT_TEXT) == PowerState(on_ac=False, battery_pct=57)
    assert parse_pmset_batt(DESKTOP_TEXT) == PowerState(on_ac=True, battery_pct=None)


def test_network_up_uses_default_route():
    ok = lambda *a, **k: subprocess.CompletedProcess(a, 0, "   route to: default\n  interface: en0\n", "")
    bad = lambda *a, **k: subprocess.CompletedProcess(a, 1, "", "route: writing to routing socket: not in table")
    assert network_up(ok) is True and network_up(bad) is False


def test_policy_matrix():
    p = SleepPolicy(battery_floor=30, net_grace_s=300)
    assert p.should_hold(PowerState(True, 5), net_ok=False, net_down_for_s=9999)[0] is True
    assert p.should_hold(PowerState(False, 30), net_ok=True, net_down_for_s=0)[0] is True
    assert p.should_hold(PowerState(False, 29), net_ok=True, net_down_for_s=0)[0] is False
    assert p.should_hold(PowerState(False, 80), net_ok=False, net_down_for_s=120)[0] is True
    assert p.should_hold(PowerState(False, 80), net_ok=False, net_down_for_s=300)[0] is False


class FakeProc:
    def __init__(self):
        self.alive = True

    def poll(self):
        return None if self.alive else 0

    def terminate(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0

    def kill(self):
        self.alive = False


def _guard(states, nets):
    t = [0.0]
    procs, events = [], []

    def spawn():
        procs.append(FakeProc())
        return procs[-1]

    g = SleepGuard(SleepPolicy(30, 300), emit=events.append,
                   read_power=lambda: states.pop(0), network_up=lambda: nets.pop(0),
                   spawn=spawn, clock=lambda: t[0])
    return g, t, procs, events


def test_guard_acquires_releases_and_emits_on_change_only():
    states = [PowerState(True, 100), PowerState(False, 60), PowerState(False, 31),
              PowerState(False, 29), PowerState(False, 28), PowerState(True, 28)]
    g, t, procs, events = _guard(states, [True] * 6)
    results = []
    for _ in range(6):
        results.append(g.tick())
        t[0] += 30
    assert results == [True, True, True, False, False, True]
    assert len(procs) == 2 and not procs[0].alive and procs[1].alive
    # AC → 배터리(유지) → 30% 미만(해제) → AC(재개): 상태 문구가 바뀐 시점마다 1건
    assert [e["hold"] for e in events] == [True, True, False, True]
    assert events[2]["battery_pct"] == 29 and events[2]["kind"] == "power"


def test_guard_network_grace_on_battery():
    states = [PowerState(False, 80)] * 4
    g, t, procs, _ = _guard(list(states), [True, False, False, True])
    assert g.tick() is True
    t[0] += 30
    assert g.tick() is True        # 끊김 시작, 유예
    t[0] += 300
    assert g.tick() is False       # 5분 넘게 끊김 → 해제
    t[0] += 30
    assert g.tick() is True        # 복구 → 다시 방지
    assert len(procs) == 2


def test_guard_respawns_if_caffeinate_died():
    g, t, procs, _ = _guard([PowerState(True, 100)] * 2, [True, True])
    g.tick()
    procs[0].alive = False
    g.tick()
    assert len(procs) == 2 and g.holding
    g.release()
    assert not procs[1].alive
