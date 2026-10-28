"""pastofi 코인판 trader: 페이퍼 브로커로 진입·청산·수수료·한도·정지선·복구. 네트워크·DB 없음."""
import json
from datetime import date

import pytest

from src.execution.paper import PaperExchange
from src.execution.types import FeeSchedule, Side
from src.strategies.pastofi_crypto.brokers import LiveMarketView
from src.strategies.pastofi_crypto.engine import LOGIC1, Trade
from src.strategies.pastofi_crypto.feed import DAY, Exit, Signal
from src.strategies.pastofi_crypto.runner import _risk_guard
from src.strategies.pastofi_crypto.spec import SPEC_V1, Spec, SpecError, SymbolFilters, order_qty, sized
from src.strategies.pastofi_crypto.store import Store
from src.strategies.pastofi_crypto.trader import StartRefused, Trader

DAY0 = (date(2026, 10, 26) - date(1970, 1, 1)).days
FILTERS = {"BTCUSDT": SymbolFilters("BTCUSDT", 0.001, 0.001, 50.0, 0.1),
           "ETHUSDT": SymbolFilters("ETHUSDT", 0.001, 0.001, 20.0, 0.01),
           "SOLUSDT": SymbolFilters("SOLUSDT", 0.01, 0.01, 5.0, 0.01),
           "EWYUSDT": SymbolFilters("EWYUSDT", 0.01, 0.01, 5.0, 0.01)}


@pytest.fixture(scope="module")
def spec():
    return Spec.load(SPEC_V1)        # 아래 거래 흐름 테스트는 v1 수량(종목별 최소 명목) 기준. v2 수량은 test_equal_notional_*


@pytest.fixture(scope="module")
def spec2():
    return Spec.load()


def test_sizing_matches_user_table(spec):
    assert order_qty(82000, FILTERS["BTCUSDT"], 1.1) == 0.001
    assert order_qty(2401.45, FILTERS["ETHUSDT"], 1.1) == 0.010
    assert order_qty(104.51, FILTERS["SOLUSDT"], 1.1) == 0.06
    assert order_qty(158.0, FILTERS["EWYUSDT"], 1.1) == 0.04
    q = order_qty(30000, FILTERS["BTCUSDT"], 1.1)
    assert q * 30000 >= 55 and (q - 0.001) * 30000 < 55
    with pytest.raises(SpecError):
        sized(spec, "SOLUSDT", {"SOLUSDT": 20.0}, FILTERS)          # 0.28 > 상한 0.12


PRICES = {"BTCUSDT": 81133.5, "ETHUSDT": 2355.8, "SOLUSDT": 102.17, "EWYUSDT": 163.76}   # 2026-10-29 bid


def test_equal_notional_sizes_all_symbols_to_btc_minimum(spec2):
    q = {s: sized(spec2, s, PRICES, FILTERS) for s in spec2.symbols}
    assert q == {"BTCUSDT": 0.001, "ETHUSDT": 0.034, "SOLUSDT": 0.79, "EWYUSDT": 0.5}
    notional = {s: q[s] * PRICES[s] for s in q}
    target = notional["BTCUSDT"]
    for s, n in notional.items():                                      # 가장 가까운 step 배수 → 반 step 안
        assert abs(n - target) <= FILTERS[s].step * PRICES[s] / 2 + 1e-9, s


def test_equal_notional_target_follows_largest_minimum(spec2):
    # BTC 가 $40,000 이면 0.001 = $40 < $55 → BTC 최소가 0.002($80). 목표도 $80 로 따라간다
    p = {**PRICES, "BTCUSDT": 40000.0}
    assert sized(spec2, "BTCUSDT", p, FILTERS) == 0.002
    assert sized(spec2, "SOLUSDT", p, FILTERS) == 0.78
    # 어떤 종목도 자기 최소 수량(MIN_NOTIONAL × 1.1) 아래로는 안 내려간다
    assert sized(spec2, "ETHUSDT", {**PRICES, "BTCUSDT": 20000.0}, FILTERS) * 2355.8 >= 22


def test_equal_notional_refuses_missing_price_and_cap(spec2):
    with pytest.raises(SpecError, match="가격이 없다"):
        sized(spec2, "SOLUSDT", {**PRICES, "ETHUSDT": None}, FILTERS)
    with pytest.raises(SpecError, match="상한"):
        sized(spec2, "SOLUSDT", {**PRICES, "SOLUSDT": 40.0}, FILTERS)  # $81 / 40 = 2.03 > 1.6


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def book(view, bid, ask, ts_ms, qty=10.0):
    view.on_book_top({"T": ts_ms, "E": ts_ms, "bids": [[bid, qty], [bid - 0.01, qty]], "asks": [[ask, qty], [ask + 0.01, qty]]})


class Env:
    def __init__(self, spec, tmp_path, **kw):
        self.t = Clock(DAY0 * DAY + 3600.0)
        self.views = {s: LiveMarketView(s) for s in spec.symbols}
        for s in spec.symbols:
            book(self.views[s], 100.0, 100.01, int(self.t() * 1000))
        fees = FeeSchedule(0.0, 4.0)
        self.dir = tmp_path / "paper"
        self.dir.mkdir(exist_ok=True)
        self.msgs = []
        self.ex = {s: PaperExchange(s, self.views[s], fees, state_file=self.dir / f"paper_{s}.json") for s in spec.symbols}
        self.guards = {s: _risk_guard(self.ex[s], spec, s, self.dir, self.msgs.append) for s in spec.symbols}
        self.store = Store("paper", self.dir, use_db=False)
        self.gate_reason = None
        self.trader = Trader(spec, "paper", self.guards, FILTERS, self.store, self.dir, notify=self.msgs.append,
                             gate=lambda s, g: self.gate_reason,
                             quote=lambda s: (self.views[s].book().best_bid, self.views[s].book().best_ask),
                             now=self.t, **kw)

    def signal(self, sym="SOLUSDT", T=3600, p_in=100.0):
        return Signal(sym, DAY0, T, T - 0.2, p_in, T + 300, {"past_bp": 75.0, "ofi_ratio": -1.2})

    def exit(self, sym="SOLUSDT", T=3600, p_out=99.5, reason="5분"):
        tr = Trade(LOGIC1, -1, T, T - 0.2, 100.0, T + 300.0, p_out, reason, 75.0, -1.0, 1.0, -1.2, 99.0, 100.0, 50.0, 49.0)
        return Exit(sym, DAY0, tr)


def test_round_trip_paper_fill_fees_and_pnl(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.trader.on_entry(env.signal())
    pos = env.trader.state.positions["SOLUSDT"]
    assert pos["status"] == "open" and pos["qty"] == 0.06
    assert pos["entry_px"] == pytest.approx(100.0)                     # 매도 → bid1
    assert pos["entry_fee"] == pytest.approx(0.06 * 100.0 * 4e-4)
    assert env.ex["SOLUSDT"].position().qty == pytest.approx(-0.06)
    stop = env.ex["SOLUSDT"].get_order(pos["stop_cid"])
    assert stop.stop_price == pytest.approx(101.5) and stop.status.is_open   # 비상 손절 +1.5%
    assert env.msgs[-1].startswith("진입 SOLUSDT 숏 0.06 @ 100")
    assert "── 종목별 포지션 ──" in env.msgs[-1] and "정지선" in env.msgs[-1]

    book(env.views["SOLUSDT"], 99.49, 99.5, int(env.t() * 1000) + 300_000)
    env.t.t += 300
    env.trader.on_exit(env.exit())
    assert env.trader.state.positions["SOLUSDT"] is None
    assert env.ex["SOLUSDT"].position().qty == pytest.approx(0)
    assert not env.ex["SOLUSDT"].get_order(pos["stop_cid"]).status.is_open   # 비상 손절 취소
    tr = env.store.trades()[0]
    assert tr["gross_usd"] == pytest.approx((100.0 - 99.5) * 0.06)
    assert tr["fees_usd"] == pytest.approx(0.06 * 100 * 4e-4 + 0.06 * 99.5 * 4e-4)
    assert tr["net_usd"] == pytest.approx(tr["gross_usd"] - tr["fees_usd"])
    assert tr["slip_in_bp"] == pytest.approx(0) and tr["slip_out_bp"] == pytest.approx(0)
    assert env.trader.state.cum_net == pytest.approx(tr["net_usd"])
    assert env.trader.account()["balance"] == pytest.approx(200 + tr["net_usd"])
    assert env.msgs[-1].startswith("청산 SOLUSDT 5분")
    sig = [s for s in env.store.signals() if s["t_sig"] == DAY0 * DAY + 3600][-1]
    assert sig["status"] == "traded" and sig["reason"] == "5분"


def test_gate_skip_keeps_virtual_result(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.gate_reason = "depth 지연 1500ms"
    env.trader.on_entry(env.signal())
    assert env.trader.state.positions["SOLUSDT"] is None
    env.trader.on_exit(env.exit())
    s = [x for x in env.store.signals() if x["t_sig"] == DAY0 * DAY + 3600][-1]
    assert s["status"] == "skipped" and s["skip_reason"] == "depth 지연 1500ms" and s["p_out"] == 99.5
    assert env.store.trades() == []


def test_daily_limit_per_symbol(spec, tmp_path):
    env = Env(spec, tmp_path)
    for i in range(11):
        T = 3600 + i * 400
        env.trader.on_entry(env.signal(T=T))
        env.trader.on_exit(env.exit(T=T, p_out=100.0))
    assert len(env.store.trades()) == 10
    last = [x for x in env.store.signals() if x["t_sig"] == DAY0 * DAY + 3600 + 10 * 400][-1]
    assert last["status"] == "skipped" and "하루 진입 한도" in last["skip_reason"]
    assert sum("진입 한도 10건에 도달" in m for m in env.msgs) == 1


def test_stop_line_halts_new_entries_and_resume(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.trader.on_entry(env.signal())
    book(env.views["SOLUSDT"], 189.99, 190.0, int(env.t() * 1000) + 1000)    # 극단 손실로 정지선 넘김
    env.trader.on_exit(env.exit(p_out=190.0, reason="손절"))
    st = env.trader.state
    assert st.cum_net < -5 and st.halted
    assert any("정지선 도달" in m for m in env.msgs)
    env.trader.on_entry(env.signal("ETHUSDT", T=4000))
    assert env.trader.state.positions["ETHUSDT"] is None
    (env.dir / "resume_request.json").write_text(json.dumps({"note": "확인"}))
    env.trader.periodic()
    assert not env.trader.state.halted and not (env.dir / "resume_request.json").exists()


def test_kill_file_blocks_entries(spec, tmp_path):
    env = Env(spec, tmp_path)
    (env.dir / "KILL").write_text("")
    env.trader.on_entry(env.signal())
    assert env.trader.state.positions["SOLUSDT"] is None


def test_restart_recovers_open_position_and_exits_after_deadline(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.trader.on_entry(env.signal())
    # 재시작: 같은 상태 폴더로 새로 만든다
    env2 = Env(spec, tmp_path)
    notes = env2.trader.recover(foreign_check=True)
    pos = env2.trader.state.positions["SOLUSDT"]
    assert pos["source"] == "recovered" and any("이어서 관리" in n for n in notes)
    env2.t.t += 301
    env2.trader.periodic()
    assert env2.trader.state.positions["SOLUSDT"] is None
    assert env2.store.trades()[-1]["reason"] == "5분(재시작)"


def test_recover_refuses_foreign_position(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.ex["EWYUSDT"].place_market(Side.BUY, 0.1, "overnight-e0")     # 다른 프로그램이 남긴 롱
    env2 = Env(spec, tmp_path)
    with pytest.raises(StartRefused):
        env2.trader.recover(foreign_check=True)


def test_emergency_stop_fill_is_booked(spec, tmp_path):
    env = Env(spec, tmp_path)
    env.trader.on_entry(env.signal())
    v = env.views["SOLUSDT"]
    book(v, 101.6, 101.61, int(env.t() * 1000) + 2000)
    env.t.t += 3
    import time
    v.on_trade({"T": int((time.time() + 5) * 1000), "a": 1, "p": "101.6", "q": "1", "m": False})   # PaperExchange 는 벽시계
    env.trader.periodic()
    assert env.trader.state.positions["SOLUSDT"] is None
    assert env.store.trades()[-1]["reason"] == "비상손절"


def test_client_ids_fit_binance_limit(spec, tmp_path):
    env = Env(spec, tmp_path)
    cid = env.trader._cid("BTCUSDT", DAY0 * DAY + 86399, "x", 12)
    assert cid == "pof-BTC-20261026-86399-x12" and len(cid) <= 36


def test_equal_notional_entry_uses_other_symbols_bids(spec2, tmp_path):
    env = Env(spec2, tmp_path)
    ms = int(env.t() * 1000)
    for s, px in PRICES.items():
        book(env.views[s], px, px + FILTERS[s].tick, ms)
    env.trader.on_entry(env.signal("SOLUSDT", p_in=PRICES["SOLUSDT"]))
    pos = env.trader.state.positions["SOLUSDT"]
    assert pos["status"] == "open" and pos["qty"] == 0.79              # ≈ BTC 0.001 명목 ($81)
    q = env.trader.quote
    env.trader.quote = lambda s: None if s == "ETHUSDT" else q(s)     # 호가 없음 (App.quote) → 수량을 못 정하니 건너뛴다
    env.trader.on_entry(env.signal("EWYUSDT", T=3700, p_in=PRICES["EWYUSDT"]))
    assert env.trader.state.positions["EWYUSDT"] is None
    s = [x for x in env.store.signals() if x["symbol"] == "EWYUSDT"][-1]
    assert s["status"] == "skipped" and s["skip_reason"] == "시세 없음 (수량 계산)"
