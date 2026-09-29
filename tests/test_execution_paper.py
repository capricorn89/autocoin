"""페이퍼 거래소 체결 모델 (WOO-24). 네트워크·DB 없음."""
from datetime import datetime, timedelta, timezone

import pytest

from src.execution.base import OrderRejected, make_broker
from src.execution.market import StaleMarketData
from src.execution.paper import PaperExchange
from src.execution.types import BookTop, FeeSchedule, Liquidity, Side, Status, TradePrint

T0 = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)


class FakeMarket:
    def __init__(self):
        self.bids = [(100.00, 2.0), (99.99, 5.0)]
        self.asks = [(100.01, 0.5), (100.02, 5.0)]
        self.trades: list[TradePrint] = []
        self.stale = False

    def book(self):
        if self.stale:
            raise StaleMarketData("stale")
        return BookTop(T0, list(self.bids), list(self.asks))

    def trades_after(self, agg_id, since):
        return [t for t in self.trades if (agg_id is None and t.ts > since) or (agg_id is not None and t.agg_id > agg_id)]

    def last_price(self):
        return self.trades[-1].price if self.trades else 100.0

    def print(self, sec, price, qty=1.0, buyer_maker=True):
        self.trades.append(TradePrint(T0 + timedelta(seconds=sec), len(self.trades) + 1, price, qty, buyer_maker))


class Clock:
    def __init__(self):
        self.t = T0

    def __call__(self):
        return self.t


@pytest.fixture
def env(tmp_path):
    m, c = FakeMarket(), Clock()
    ex = PaperExchange("EWYUSDT", m, FeeSchedule(maker_bps=0.0, taker_bps=4.0), clock=c,
                       state_file=tmp_path / "paper.json")
    return ex, m, c


def test_market_buy_walks_book_and_pays_taker(env):
    ex, m, _ = env
    o = ex.place_market(Side.BUY, 1.0, "e1")
    assert o.status is Status.FILLED
    assert o.avg_price == pytest.approx((0.5 * 100.01 + 0.5 * 100.02) / 1.0)
    f = ex.fills()[0]
    assert f.liquidity is Liquidity.TAKER and f.fee == pytest.approx(o.avg_price * 4e-4)
    assert ex.position().qty == 1.0


def test_gtx_that_would_cross_is_expired(env):
    ex, _, _ = env
    o = ex.place_limit_gtx(Side.BUY, 1.0, 100.01, "b1")
    assert o.status is Status.EXPIRED and "GTX" in o.reason
    assert ex.position().qty == 0


def test_gtx_fills_only_when_trade_goes_through(env):
    ex, m, c = env
    o = ex.place_limit_gtx(Side.BUY, 1.0, 100.00, "b1")
    assert o.status is Status.NEW
    m.print(1, 100.00)                       # 같은 가격 — 대기열 모름, 체결 아님
    ex.poll()
    assert o.status is Status.NEW and ex.touched_at("b1") == T0 + timedelta(seconds=1)
    m.print(2, 99.99)                        # 뚫고 지나감
    ex.poll()
    assert o.status is Status.FILLED and o.avg_price == 100.00
    assert ex.fills()[0].liquidity is Liquidity.MAKER and ex.fills()[0].fee == 0


def test_trades_before_placement_do_not_fill(env):
    ex, m, c = env
    m.print(-5, 99.50)
    ex.place_limit_gtx(Side.BUY, 1.0, 100.00, "b1")
    ex.poll()
    assert ex.get_order("b1").status is Status.NEW


def test_reduce_only_rejects_increase_and_flip(env):
    ex, _, _ = env
    assert ex.place_market(Side.SELL, 1.0, "x0", reduce_only=True).status is Status.EXPIRED
    ex.place_market(Side.BUY, 1.0, "e1")
    assert ex.place_market(Side.BUY, 1.0, "x1", reduce_only=True).status is Status.EXPIRED
    assert ex.place_market(Side.SELL, 2.0, "x2", reduce_only=True).status is Status.EXPIRED
    assert ex.place_market(Side.SELL, 1.0, "x3", reduce_only=True).status is Status.FILLED
    assert ex.position().qty == 0


def test_resting_reduce_only_expires_if_position_gone(env):
    ex, m, _ = env
    ex.place_market(Side.BUY, 1.0, "e1")
    ex.place_limit_gtx(Side.SELL, 1.0, 100.05, "x1", reduce_only=True)
    ex.place_market(Side.SELL, 1.0, "x2", reduce_only=True)     # 다른 경로로 먼저 청산
    m.print(3, 100.10)
    ex.poll()
    assert ex.get_order("x1").status is Status.EXPIRED


def test_stop_market_triggers_and_closes(env):
    ex, m, _ = env
    ex.place_market(Side.BUY, 1.0, "e1")
    s = ex.place_stop_market(Side.SELL, 1.0, 88.0, "sl")
    assert s.status is Status.NEW and s.reduce_only
    m.print(1, 90.0); ex.poll()
    assert s.status is Status.NEW
    m.bids = [(87.5, 5.0)]
    m.print(2, 87.9); evs = ex.poll()
    assert s.status is Status.FILLED and s.avg_price == 87.5
    assert [e.kind for e in evs] == ["triggered", "filled"]
    assert ex.position().qty == 0
    assert ex.position().realized_pnl == pytest.approx(87.5 - 100.015)


def test_stop_already_through_is_rejected(env):
    ex, m, _ = env
    ex.place_market(Side.BUY, 1.0, "e1")
    m.print(1, 87.0)
    assert ex.place_stop_market(Side.SELL, 1.0, 88.0, "sl").status is Status.REJECTED


def test_duplicate_client_id_is_idempotent(env):
    ex, _, _ = env
    a = ex.place_market(Side.BUY, 1.0, "e1")
    b = ex.place_market(Side.BUY, 1.0, "e1")
    assert a is b and len(ex.fills()) == 1


def test_cancel_and_cancel_all(env):
    ex, _, _ = env
    ex.place_limit_gtx(Side.BUY, 1.0, 99.0, "b1")
    ex.place_limit_gtx(Side.BUY, 1.0, 98.0, "b2")
    assert ex.cancel("b1").status is Status.CANCELED
    with pytest.raises(OrderRejected):
        ex.cancel("b1")
    assert [o.client_id for o in ex.cancel_all()] == ["b2"]
    assert ex.open_orders() == []


def test_stale_market_blocks_market_order(env):
    ex, m, _ = env
    m.stale = True
    with pytest.raises(StaleMarketData):
        ex.place_market(Side.BUY, 1.0, "e1")


def test_state_survives_restart(env, tmp_path):
    ex, m, c = env
    ex.place_market(Side.BUY, 1.0, "e1")
    ex.place_limit_gtx(Side.SELL, 1.0, 100.05, "x1", reduce_only=True)
    ex2 = PaperExchange("EWYUSDT", m, FeeSchedule(0.0, 4.0), clock=c, state_file=tmp_path / "paper.json")
    assert ex2.position().qty == 1.0
    assert [o.client_id for o in ex2.open_orders()] == ["x1"]
    m.print(5, 100.10); ex2.poll()
    assert ex2.position().qty == 0 and len(ex2.fills()) == 2


def test_make_broker_requires_confirm_for_live():
    with pytest.raises(OrderRejected):
        make_broker("live")
    with pytest.raises(ValueError):
        make_broker("real")
