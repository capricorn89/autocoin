"""리스크 가드 (WOO-26). 페이퍼 거래소 위에서 검증. 네트워크·DB 없음."""
from datetime import datetime, timezone

import pytest

from src.execution.market import StaleMarketData
from src.execution.paper import PaperExchange
from src.execution.risk import RiskGuard, RiskHalt, RiskLimits
from src.execution.types import BookTop, FeeSchedule, Side, Status
from src.overnight.spec import Spec

T0 = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)


class Market:
    stale = False

    def book(self):
        if self.stale:
            raise StaleMarketData("stale")
        return BookTop(T0, [(100.0, 5.0)], [(100.01, 5.0)])

    def trades_after(self, agg_id, since):
        return []

    def last_price(self):
        return 100.0


@pytest.fixture
def env(tmp_path):
    alerts = []
    m = Market()
    ex = PaperExchange("EWYUSDT", m, FeeSchedule(0.0, 4.0), clock=lambda: T0)
    g = RiskGuard(ex, RiskLimits.from_spec(Spec.load().raw["risk"]), tmp_path / "risk.json",
                  tmp_path / "KILL", alert=alerts.append, clock=lambda: T0)
    return g, m, alerts, tmp_path


def test_limits_come_from_spec():
    lim = RiskLimits.from_spec(Spec.load().raw["risk"])
    assert lim.max_abs_position == 1.0 and lim.test_stop_cumulative_usd == -37.0
    assert lim.pause_after_consecutive_losses == 8 and lim.max_consecutive_rejects == 2


def test_position_cap_blocks_increase(env):
    g, _, alerts, _ = env
    g.place_market(Side.BUY, 1.0, "e1")
    with pytest.raises(RiskHalt, match="상한"):
        g.place_market(Side.BUY, 0.01, "e2")
    assert alerts and g.position().qty == 1.0


def test_position_cap_counts_resting_orders(env):
    g, _, _, _ = env
    g.place_limit_gtx(Side.BUY, 1.0, 99.0, "b1")
    with pytest.raises(RiskHalt):
        g.place_limit_gtx(Side.BUY, 1.0, 98.0, "b2")


def test_flip_is_blocked_but_close_allowed(env):
    g, _, _, _ = env
    g.place_market(Side.BUY, 1.0, "e1")
    with pytest.raises(RiskHalt):
        g.place_market(Side.SELL, 2.5, "x1")            # 청산 + 1.5 숏 → 상한 초과
    assert g.place_market(Side.SELL, 1.0, "x2").status is Status.FILLED


def test_cumulative_loss_halts_and_blocks_entry_but_not_exit(env):
    g, _, alerts, _ = env
    g.place_market(Side.BUY, 1.0, "e1")
    g.record_night("2026-10-01", -20.0)
    g.record_night("2026-10-02", -17.5)
    assert g.state.status == "halted" and "테스트 중단" in g.state.reason
    assert not g.can_enter()
    assert g.place_market(Side.SELL, 1.0, "x1", reduce_only=True).status is Status.FILLED
    with pytest.raises(RiskHalt):
        g.place_market(Side.BUY, 1.0, "e2")


def test_consecutive_losses_pause_then_resume(env):
    g, _, _, _ = env
    for i in range(8):
        g.record_night(f"n{i}", -0.1)
    assert g.state.status == "paused"
    g.record_night("n9", +1.0)                           # 이익이 나도 사람이 재개해야 한다
    assert g.state.status == "paused"
    g.resume("확인함")
    assert g.can_enter() and g.state.consecutive_losses == 0


def test_kill_switch_file(env):
    g, _, _, tmp = env
    (tmp / "KILL").touch()
    assert not g.can_enter() and g.state.status == "halted"
    with pytest.raises(RiskHalt):
        g.reset_halt("킬 파일 남아 있음")
    (tmp / "KILL").unlink()
    g.reset_halt("원인 확인")
    assert g.can_enter()


def test_health_checks_halt(env):
    g, _, _, _ = env
    g.check_health(clock_offset_ms=120, data_age_s=2)
    assert g.state.status == "active"
    g.check_health(clock_offset_ms=1500)
    assert g.state.status == "halted" and "시계" in g.state.reason


def test_stale_market_during_order_halts(env):
    g, m, _, _ = env
    m.stale = True
    with pytest.raises(StaleMarketData):
        g.place_market(Side.BUY, 1.0, "e1")
    assert g.state.status == "halted"


def test_consecutive_rejects_halt(env):
    g, _, _, _ = env
    g.place_market(Side.BUY, 1.0, "e1")
    # 손절가가 이미 지난 가격 → 페이퍼가 REJECTED
    g.place_stop_market(Side.SELL, 1.0, 101.0, "s1")
    assert g.state.status == "active" and g.state.reject_streak == 1
    g.place_stop_market(Side.SELL, 1.0, 101.0, "s2")
    assert g.state.status == "halted" and "거부 2연속" in g.state.reason


def test_stop_must_reduce(env):
    g, _, _, _ = env
    with pytest.raises(RiskHalt):
        g.place_stop_market(Side.SELL, 1.0, 88.0, "s1")  # 포지션 없음


def test_position_mismatch_halts(env):
    g, _, _, _ = env
    g.check_position(expected_qty=1.0)
    assert g.state.status == "halted" and "불일치" in g.state.reason


def test_state_persists(env, tmp_path):
    g, m, _, _ = env
    g.record_night("n1", -5.0)
    g.halt("테스트")
    g2 = RiskGuard(g.broker, g.limits, tmp_path / "risk.json", tmp_path / "KILL", alert=lambda s: None)
    assert g2.state.status == "halted" and g2.state.cumulative_pnl == -5.0
