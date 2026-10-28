"""pastofi 코인판 실시간 경로(SymbolFeed) = 백테스트 경로(run_all.py) 일치.

1) 합성 데이터: SymbolFeed 로 흘린 결과 = run_all.py 식으로 엔진에 직접 넣은 결과 (네트워크·DB 없음)
2) 실데이터(필수 일치 검증): DB 를 수집기 레코드 모양으로 바꿔 SymbolFeed 로 재생 → all_trades_*.csv 의 A·로직1 59건을
   거래 단위(t_sig, p_in, p_out, reason)로 똑같이 재현. DB 가 없으면 건너뛴다. 이게 통과해야 페이퍼를 켠다.
"""
import math
from datetime import date

import pytest

from src.strategies.pastofi_crypto.engine import LOGIC1
from src.strategies.pastofi_crypto.feed import DAY, SymbolFeed
from src.strategies.pastofi_crypto.replay import compare, expected_trades, run_day
from src.strategies.pastofi_crypto.spec import Spec

D = date(2026, 10, 26)
DAY0 = (D - date(1970, 1, 1)).days
BASE_MS = DAY0 * DAY * 1000


@pytest.fixture(scope="module")
def spec():
    return Spec.load()


def synthetic(scale_ofi: float = 0.5):
    """1000초까지 100 근처 횡보 → 1290초까지 +1.2% 상승 → 횡보하며 매수 잔량 감소·매도 잔량 증가(OFI 음수).
    체결은 초마다 매수·매도 0.5개씩 (분당 60개). 반환: (호가 [(ms, b, bq, a, aq)], 체결 [(ms, q, m)])."""
    quotes, trades = [], []
    bq, aq = 50.0, 50.0
    for i in range(0, 2400):
        t = i
        if i < 1000:
            mid = 100.0 + (0.01 if i % 2 else 0.0)
        elif i <= 1290:
            mid = 100.0 + 1.2 * (i - 1000) / 290
        else:
            mid = 101.2
            bq = max(1.0, bq - scale_ofi)
            aq = aq + scale_ofi
        b, a = round(mid - 0.005, 3), round(mid + 0.005, 3)
        quotes.append((BASE_MS + t * 1000 + 250, b, bq, a, aq))
        quotes.append((BASE_MS + t * 1000 + 600, b, bq, a, aq))      # 1호가 같은 레코드 (5호가만 바뀐 경우) → 버려져야 한다
        trades.append((BASE_MS + t * 1000 + 100, 0.5, True))
        trades.append((BASE_MS + t * 1000 + 700, 0.5, False))
    return quotes, trades


def direct(spec, quotes, trades):
    """run_all.py 의 루프를 그대로 옮긴 기준 구현 (엔진에 직접)."""
    S = spec.qty_scale
    q = [(ms / 1000 - DAY0 * DAY, b, bq, a, aq) for ms, b, bq, a, aq in quotes]
    keep = [q[0]] + [r for p, r in zip(q, q[1:]) if r[1:] != p[1:]]
    vol = {}
    for ms, qty, m in trades:
        k = (ms // 1000 - DAY0 * DAY, -1 if m else 1)
        vol[k] = vol.get(k, 0.0) + qty
    va = sorted(vol.items())
    e = spec.new_engine((keep[0][1] + keep[0][3]) / 2)
    qi = vi = 0
    for T in range(DAY):
        while qi < len(keep) and keep[qi][0] <= T:
            t, b, bq, a, aq = keep[qi]
            e.on_quote(t, b, bq * S, a, aq * S, True, True)
            e.on_fut_quote(t, b, bq * S, a, aq * S)
            qi += 1
        while vi < len(va) and va[vi][0][0] <= T - 1:
            (s, side), qty = va[vi]
            e.on_trade(s, int(round(qty * S)), side)
            vi += 1
        e.advance(T)
    e.finish()
    return [t for t in e.trades if t.logic == LOGIC1]


def test_feed_matches_direct_engine_on_synthetic_day(spec):
    quotes, trades = synthetic()
    ref = direct(spec, quotes, trades)
    got = run_day(spec, "SOLUSDT", D, quotes, trades)
    assert len(ref) >= 1, "합성 데이터에서 신호가 나야 한다"
    assert [(t.t_sig, t.p_in, t.p_out, t.reason, round(t.t_in, 6)) for t in ref] == \
           [(t["t_sig"], t["p_in"], t["p_out"], t["reason"], round(t["t_in"], 6)) for t in got]
    first = ref[0]
    assert first.past_bp >= 70 and first.ofi_ratio < -0.5 and first.side == -1


def test_feed_emits_entry_then_exit_in_order(spec):
    quotes, trades = synthetic()
    ev = []
    feed = SymbolFeed(spec, "SOLUSDT", lambda s: ev.append(("in", s.t_sig, s.p_in)),
                      lambda x: ev.append(("out", x.t_sig, x.trade.reason)))
    import heapq
    nxt = 0
    for ts, kind, r in heapq.merge(((r[0], 0, r) for r in quotes), ((r[0], 1, r) for r in trades)):
        while BASE_MS + nxt * 1000 + 300 <= ts:
            feed.tick(DAY0, nxt)
            nxt += 1
        if kind == 0:
            feed.on_l1(*r)
        else:
            feed.on_trade_raw(*r)
    while nxt < DAY:
        feed.tick(DAY0, nxt)
        nxt += 1
    feed.finish_day(DAY0)
    assert ev and ev[0][0] == "in"
    ins = [e for e in ev if e[0] == "in"]
    outs = [e for e in ev if e[0] == "out"]
    assert [e[1] for e in ins] == [e[1] for e in outs]
    for a, b in zip(ev[::2], ev[1::2]):         # 진입 → 청산이 번갈아 나온다 (종목당 하나)
        assert a[0] == "in" and b[0] == "out" and a[1] == b[1]


def test_quantity_scale_keeps_ratio(spec):
    """잔량·체결량 둘 다 ×1000 → OFI 비율은 그대로. 소수 체결량이 int 로 잘려 0 이 되지 않는다."""
    feed = SymbolFeed(spec, "BTCUSDT")
    feed.on_l1(BASE_MS + 1000, 100.0, 0.004, 100.1, 0.003)
    feed.on_trade_raw(BASE_MS + 1500, 0.003, False)
    feed.tick(DAY0, 3)
    e = feed.engine(DAY0)
    assert e._vcum[-1] == 3                              # 0.003 × 1000
    assert e._last_q[1] == pytest.approx(4.0) and e._last_q[3] == pytest.approx(3.0)


def test_trades_are_fed_only_up_to_T_minus_1(spec):
    feed = SymbolFeed(spec, "ETHUSDT")
    feed.on_l1(BASE_MS + 10, 10.0, 1.0, 10.1, 1.0)
    feed.on_trade_raw(BASE_MS + 4999, 1.0, True)        # 초 4
    feed.on_trade_raw(BASE_MS + 5001, 2.0, False)       # 초 5
    feed.tick(DAY0, 5)                                  # 초 ≤ 4 만
    e = feed.engine(DAY0)
    assert e._vsec == [4] and e._vcum == [1000]
    feed.tick(DAY0, 6)
    assert e._vsec == [4, 5] and e._vcum == [1000, 3000]


def test_new_engine_per_utc_day(spec):
    feed = SymbolFeed(spec, "EWYUSDT")
    feed.on_l1(BASE_MS + DAY * 1000 - 500, 50.0, 1.0, 50.01, 1.0)
    feed.on_l1(BASE_MS + DAY * 1000 + 500, 50.0, 1.0, 50.01, 1.0)   # 다음 날 첫 레코드는 값이 같아도 들어간다
    assert feed.engine(DAY0) is not feed.engine(DAY0 + 1)
    assert feed.engine(DAY0 + 1).n_quotes == 1
    feed.finish_day(DAY0)
    assert feed.engine(DAY0) is None
    feed.on_l1(BASE_MS + 100, 49.0, 1.0, 49.01, 1.0)                  # 끝난 날 레코드는 버린다
    assert feed.engine(DAY0) is None


def test_engine_params_are_spec_values(spec):
    p = spec.params()
    assert (p.move_bp, p.tp_bp, p.sl_bp, p.hold, p.ofi_th, p.move_win, p.ofi_win, p.vol_win) == \
           (70.0, 200.0, 100.0, 300, 0.5, 300, 60, 600)
    assert (p.session_open, p.session_close, p.entry_start, p.entry_end) == (0, 86400, 0, 86100)
    assert (p.fut_min_rem, p.fut_gap_ticks, p.fut_limit_bp, p.vi_pad) == (0, 10 ** 9, 1e9, 0)
    assert p.version == spec.version


# ---------------------------------------------------------------- 실데이터 일치 검증 (필수)
def _db_ok() -> bool:
    try:
        import psycopg
        from src.storage.db import get_dsn
        with psycopg.connect(get_dsn(None), connect_timeout=3) as c:
            return c.execute("SELECT count(*) FROM market.book_top5 WHERE symbol = 'BTCUSDT' AND exchange_ts "
                             ">= '2026-10-13' AND exchange_ts < '2026-10-13 00:10+00'").fetchone()[0] > 0
    except Exception:
        return False


@pytest.mark.skipif(not _db_ok(), reason="수집 DB 없음")
def test_replay_reproduces_backtest_A_logic1_59_trades():
    from src.strategies.pastofi_crypto.replay import check
    exp = expected_trades()
    assert len(exp) == 59
    r = check()
    assert r["missing"] == [] and r["extra"] == [] and r["diff"] == []
    assert r["match"] == r["expected"] == r["got"] == 59
    # 재생 결과의 피처까지 백테스트와 같다 (부동소수 허용오차)
    key = lambda x: (x["sym"], x["day"], int(float(x["t_sig"])))
    got = {key(t): t for t in r["trades"]}
    for e in exp:
        g = got[key(e)]
        assert math.isclose(float(e["past_bp"]), g["past_bp"], rel_tol=1e-9)
        assert math.isclose(float(e["ofi_ratio"]), g["ofi_ratio"], rel_tol=1e-9)
        assert math.isclose(float(e["t_in"]), g["t_in"], abs_tol=1e-6)
        assert math.isclose(float(e["t_out"]), g["t_out"], abs_tol=1e-6)


def test_compare_flags_differences():
    a = [{"sym": "X", "day": "d", "t_sig": "10", "p_in": "1", "p_out": "2", "reason": "5분"}]
    assert compare(a, a)["ok"]
    b = [{**a[0], "reason": "익절"}]
    r = compare(a, b)
    assert not r["ok"] and r["diff"] == [("X", "d", 10)]
    assert compare(a, [])["missing"] == [("X", "d", 10)]
