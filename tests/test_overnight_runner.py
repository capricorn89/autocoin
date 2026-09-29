"""오버나잇 스케줄러 (WOO-98) — 가짜 시계·가격 경로로 밤 전체를 돌린다. 네트워크·DB 없음."""
import json
from datetime import date, datetime, timedelta, timezone

import pytest

from src.execution.paper import PaperExchange
from src.execution.risk import RiskGuard, RiskLimits
from src.execution.types import BookTop, FeeSchedule, Liquidity, Side, TradePrint
from src.overnight.runner import Runner, calendar_diffs
from src.overnight.spec import SPEC_PATH, Spec

UTC = timezone.utc
SPEC = Spec.load()


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t

    def sleep(self, s):
        self.t += timedelta(seconds=s)


class SimMarket:
    """path(t) = 최우선 매수호가. 매도호가 = +0.01. 10초마다 path(t) 가격으로 체결이 찍힌다."""

    def __init__(self, clock, path):
        self.clock, self.path = clock, path

    def book(self):
        p = self.path(self.clock())
        return BookTop(self.clock(), [(p, 50.0)], [(round(p + 0.01, 2), 50.0)])

    def trades_after(self, agg_id, since):
        start = since if agg_id is None else datetime.fromtimestamp(agg_id, UTC)
        t = start.replace(microsecond=0) + timedelta(seconds=10 - start.second % 10)
        out = []
        while t <= self.clock():
            out.append(TradePrint(t, int(t.timestamp()), self.path(t), 1.0, True))
            t += timedelta(seconds=10)
        return out

    def last_price(self):
        return self.path(self.clock())


def make(tmp_path, start, path, funding=0.0, spec=SPEC, exchange_cls=PaperExchange):
    c = Clock(start)
    m = SimMarket(c, path)
    ex = exchange_cls("EWYUSDT", m, FeeSchedule(0.0, 4.0), clock=c, state_file=tmp_path / "paper.json")
    alerts = []
    g = RiskGuard(ex, RiskLimits.from_spec(spec.raw["risk"]), tmp_path / "risk.json", tmp_path / "KILL",
                  alert=alerts.append, clock=c)
    r = Runner(spec, g, m, "paper", tmp_path, notify=alerts.append, health=lambda: {"clock_offset_ms": 0.0},
               funding=lambda s, e, q: funding, clock=c, sleep=c.sleep, calendar_check=lambda s: [])
    return r, c, m, alerts


def kst(d, hm):
    return datetime.fromisoformat(f"{d} {hm}+09:00").astimezone(UTC)


def night(d):
    return SPEC.night_for_entry(date.fromisoformat(d))


def history(tmp_path):
    return json.loads((tmp_path / "runner.json").read_text())["history"]


def test_night_a_market_both_legs(tmp_path):
    n = night("2026-10-12")
    assert n.method == "A"
    path = lambda t: 100.0 if t < kst("2026-10-13", "08:00") else 101.0
    r, c, _, _ = make(tmp_path, kst("2026-10-12", "15:00"), path, funding=0.05)
    r.run_night(n)
    h = history(tmp_path)["2026-10-12"]
    assert h["phase"] == "done" and h["qty"] == 1.0
    assert h["entry_price"] == pytest.approx(100.01) and h["exit_price"] == pytest.approx(101.0)
    assert h["stop_price"] == pytest.approx(round(100.01 * 0.88, 2))
    fees = (100.01 + 101.0) * 4e-4
    assert h["pnl"] == pytest.approx(101.0 - 100.01 - fees + 0.05)
    assert r.broker.position().qty == 0 and r.broker.open_orders() == []
    assert r.broker.state.cumulative_pnl == pytest.approx(h["pnl"])
    fills = r.broker.fills()
    assert fills[0].ts == kst("2026-10-12", "15:30") and fills[-1].ts == kst("2026-10-13", "08:59")


def test_night_b_maker_entry_then_market_fallback_exit(tmp_path):
    n = night("2026-10-13")
    assert n.method == "B"

    def path(t):
        if t < kst("2026-10-13", "15:30:05"):
            return 100.0
        if t < kst("2026-10-14", "08:00"):
            return 99.99                                  # 15:30:05 부터 우리 매수가 100 을 뚫고 내려감
        return 101.0                                      # 청산 창: 우리 매도가 101.01 위로는 체결 없음
    r, c, _, _ = make(tmp_path, kst("2026-10-13", "15:00"), path)
    r.run_night(n)
    h = history(tmp_path)["2026-10-13"]
    buys = [f for f in r.broker.fills() if f.side is Side.BUY]
    sells = [f for f in r.broker.fills() if f.side is Side.SELL]
    assert buys[0].liquidity is Liquidity.MAKER and buys[0].price == 100.0 and buys[0].fee == 0
    assert sells[0].liquidity is Liquidity.TAKER and sells[0].ts == kst("2026-10-14", "08:59:30")
    assert h["pnl"] == pytest.approx(101.0 - 100.0 - 101.0 * 4e-4)


def test_stop_triggers_during_hold(tmp_path):
    n = night("2026-10-12")
    path = lambda t: 100.0 if t < kst("2026-10-12", "20:00") else 85.0
    r, _, _, alerts = make(tmp_path, kst("2026-10-12", "15:00"), path)
    r.run_night(n)
    h = history(tmp_path)["2026-10-12"]
    assert h["phase"] == "done" and "손절" in h["notes"][-1]
    assert h["pnl"] < -10 and r.broker.position().qty == 0
    assert any("비상 손절 발동" in a for a in alerts)


def test_holiday_night_is_skipped_without_orders(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-02", "15:00"), lambda t: 100.0)
    r.run_night(night("2026-10-02"))
    assert history(tmp_path)["2026-10-02"]["phase"] == "skipped"
    assert r.broker.fills() == []


def test_halted_risk_skips_entry(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.broker.halt("테스트")
    r.run_night(night("2026-10-12"))
    h = history(tmp_path)["2026-10-12"]
    assert h["phase"] == "skipped" and "halted" in h["notes"][-1]
    assert r.broker.fills() == []


def test_late_start_after_give_up_skips(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-12", "15:45"), lambda t: 100.0)
    r.run_night(night("2026-10-12"))
    assert "지남" in history(tmp_path)["2026-10-12"]["notes"][-1]


def test_restart_while_holding_resumes_and_exits(tmp_path):
    n = night("2026-10-12")
    r, c, m, _ = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.enter(n)
    assert r.night.phase == "holding"
    c.t = kst("2026-10-13", "02:00")                     # 새벽에 프로세스 재시작
    r2 = Runner(SPEC, r.broker, m, "paper", tmp_path, notify=lambda s: None,
                health=lambda: {"clock_offset_ms": 0.0}, funding=lambda s, e, q: 0.0,
                clock=c, sleep=c.sleep, calendar_check=lambda s: [])
    r2.recover()
    assert r2.night.phase == "holding"
    r2.run_night(n)
    assert history(tmp_path)["2026-10-12"]["phase"] == "done" and r2.broker.position().qty == 0


def test_restart_recreates_missing_stop(tmp_path):
    n = night("2026-10-12")
    r, c, m, alerts = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.enter(n)
    r.broker.cancel(r.night.stop_id)
    r.recover()
    assert any("손절 주문이 없어" in a for a in alerts)
    assert any(o.type.value == "STOP_MARKET" for o in r.broker.open_orders())


def test_unknown_position_at_start_halts_without_closing(tmp_path):
    r, _, _, alerts = make(tmp_path, kst("2026-10-12", "10:00"), lambda t: 100.0)
    r.broker.broker.place_market(Side.BUY, 1.0, "manual")   # 가드 밖에서 생긴 포지션
    r.recover()
    assert r.broker.state.status == "halted" and r.broker.position().qty == 1.0


def test_calendar_diffs_against_xkrx_in_cover_range():
    assert calendar_diffs(SPEC) == []


def test_calendar_mismatch_skips_affected_night(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.bad_dates = {"2026-10-13"}
    r.run_night(night("2026-10-12"))
    assert history(tmp_path)["2026-10-12"]["notes"][-1] == "달력 불일치"


def test_events_log_written(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.run_night(night("2026-10-12"))
    kinds = [json.loads(l)["event"] for l in (tmp_path / "events.jsonl").read_text().splitlines()]
    assert "placed" in kinds and "filled" in kinds and kinds.count("night") == 1


SPEC_V2 = Spec.load(SPEC_PATH.with_name("spec_v2.yaml"))


class PartialMakerExchange(PaperExchange):
    """GTX 가 뚫리면 남은 수량의 80% 만 체결 (부분 체결 재현)."""

    def _maybe_fill_limit(self, o, t):
        through = t.price < o.price if o.side is Side.BUY else t.price > o.price
        if through and o.filled_qty == 0:
            self._record_fill(o, round(o.remaining * 0.8, 8), o.price, Liquidity.MAKER, t.ts)
        elif through:
            return


def test_v2_live_size_and_sub_min_notional_remainder_dropped(tmp_path):
    n = SPEC_V2.night_for_entry(date(2026, 10, 13))
    assert n.method == "B" and SPEC_V2.raw["qty"] == 0.1

    def path(t):
        if t < kst("2026-10-13", "15:30:05"):
            return 100.0
        return 99.99 if t < kst("2026-10-14", "08:00") else 101.0
    r, _, _, _ = make(tmp_path, kst("2026-10-13", "15:00"), path, spec=SPEC_V2, exchange_cls=PartialMakerExchange)
    r.run_night(n)
    h = history(tmp_path)["2026-10-13"]
    # 0.08 maker 체결, 잔량 0.02 × ~100 = $2 < $5 → 시장가로 채우지 않음
    assert h["qty"] == pytest.approx(0.08)
    assert any("최소 $5" in x for x in h["notes"])
    assert [f.client_id for f in r.broker.fills() if f.side is Side.BUY] == ["on-20261013-e0"]
    assert r.broker.position().qty == 0 and h["phase"] == "done"


def test_v2_market_night_uses_small_size(tmp_path):
    r, _, _, _ = make(tmp_path, kst("2026-10-12", "15:00"), lambda t: 100.0, spec=SPEC_V2)
    r.run_night(SPEC_V2.night_for_entry(date(2026, 10, 12)))
    h = history(tmp_path)["2026-10-12"]
    assert h["qty"] == pytest.approx(0.1) and r.broker.limits.max_abs_position == 0.1


# ---------------------------------------------------------------- --status
from src.overnight.runner import collect_status, status_text


def test_status_before_first_night(tmp_path):
    (tmp_path / "live").mkdir()
    txt = status_text("live", exchange=False, state_root=tmp_path, now=kst("2026-09-29", "22:00"),
                      agent={"label": "com.autocoin.overnight.live", "loaded": True, "state": "running", "pid": "1"})
    assert "v2" in txt and "수량 0.1" in txt and "첫 밤 전" in txt
    assert "2026-09-30 → 2026-10-01  [A] 진입 09-30 15:30:00 / 청산 10-01 08:59:00" in txt
    assert "스킵: 연휴 4일" in txt


def test_status_flags_unloaded_agent_and_kill(tmp_path):
    (tmp_path / "paper").mkdir()
    (tmp_path / "paper" / "KILL").touch()
    txt = status_text("paper", exchange=False, state_root=tmp_path, now=kst("2026-09-29", "22:00"),
                      agent={"label": "com.autocoin.overnight.paper", "loaded": False})
    assert "등록 안 됨" in txt and "킬 스위치" in txt


def test_status_mid_night_and_upcoming_excludes_done(tmp_path):
    r, c, _, _ = make(tmp_path / "paper", kst("2026-10-12", "15:00"), lambda t: 100.0)
    r.run_night(night("2026-10-12"))
    st = collect_status("paper", state_root=tmp_path, now=kst("2026-10-13", "12:00"),
                        agent={"label": "x", "loaded": True})
    assert st["nights_done"] == 1 and st["risk"]["cumulative_pnl"] != 0
    assert st["upcoming"][0]["entry_date"] == "2026-10-13"
    txt = status_text("paper", state_root=tmp_path, now=kst("2026-10-13", "12:00"),
                      agent={"label": "x", "loaded": True})
    assert "직전 밤    2026-10-12 [A] done, 손익" in txt and "거래소    포지션 0.0" in txt


def test_status_survives_exchange_error(tmp_path):
    (tmp_path / "live").mkdir()
    def boom():
        raise RuntimeError("network")
    st = collect_status("live", state_root=tmp_path, now=kst("2026-09-29", "22:00"),
                        agent={"label": "x", "loaded": True}, exchange=boom)
    assert "network" in st["exchange"]["error"]
