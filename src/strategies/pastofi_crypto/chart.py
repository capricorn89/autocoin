"""거래 상세 차트 데이터 — 웹에서 거래를 누르면 그 앞뒤 구간의 가격·직전 5분 수익률·1분 OFI 비율을 1초마다 보여 준다.

krx-hft report/ofi_web.py 백테스트 탭의 거래 상세(build_bt_feats)와 같은 생각이다: 엔진을 다시 재생해서 그 초에 엔진이
봤을 값을 뽑는다. 여기서는 DB(market.book_top5·market.trades)의 그 구간만 읽어 SymbolFeed(실시간·재생과 같은 경로)에
흘리고, 초마다 SymbolFeed.snapshot 으로 값을 뽑는다. 창(최대 662초)이 차도록 구간 앞 PAD 초를 더 읽는다.

엔진은 UTC 날짜마다 새로 만들어지므로 자정 직후 661초는 엔진도 판단하지 않는다 → OFI 비율을 비운다(실시간과 같다).
신호 초의 값이 거래 기록(past_bp·ofi_ratio)과 같은지 check 에 싣는다 — 다르면 데이터가 모자란 구간이다(수집 공백 등).
"""
from __future__ import annotations

import heapq
from datetime import datetime, timezone

from .feed import DAY, SymbolFeed
from .spec import Spec

BEFORE = 15 * 60          # 신호 앞
AFTER = 15 * 60           # 청산 뒤
PAD = 700                 # 피처 창(662초)이 차도록 더 읽는 앞 구간


def build(spec: Spec, symbol: str, lo: int, hi: int, quotes: list, trades: list) -> dict:
    """[lo, hi] epoch 초마다 (mid, bid, ask, past_bp, ofi_ratio). quotes·trades 는 replay.load_window 모양 (lo-PAD 부터)."""
    feed = SymbolFeed(spec, symbol)
    delay_ms = int(round(float(spec.raw["timing"]["decision_delay_s"]) * 1000))
    out = {k: [] for k in ("t", "mid", "bid", "ask", "past_bp", "ofi_ratio")}
    nxt = lo - PAD                            # 다음에 tick 할 epoch 초

    def tick_to(E: int) -> None:              # 초 E까지 tick, [lo, hi] 안이면 뽑는다 (replay.run_day 와 같은 순서)
        nonlocal nxt
        while nxt <= E:
            day, T = divmod(nxt, DAY)
            feed.tick(day, T)
            if nxt >= lo:
                _sample(feed, day, T, nxt, out)
            nxt += 1

    qs = ((r[0], 0, r) for r in quotes)
    ts_ = ((r[0], 1, r) for r in trades)
    for ts, kind, r in heapq.merge(qs, ts_, key=lambda x: (x[0], x[1])):
        tick_to(min((ts - delay_ms) // 1000, hi))
        if nxt > hi:
            break
        if kind == 0:
            feed.on_l1(r[0], float(r[1]), float(r[2]), float(r[3]), float(r[4]))
        else:
            feed.on_trade_raw(r[0], float(r[1]), bool(r[2]))
    tick_to(hi)
    return out


def _sample(feed: SymbolFeed, day: int, T: int, E: int, out: dict) -> None:
    e = feed.engine(day)
    s = feed.snapshot(day, T) if e else {}
    i = e._idx(T) if e else -1
    out["t"].append(E)
    out["mid"].append(e._mid[i] if i >= 0 else None)
    out["bid"].append(e._bid[i] if i >= 0 else None)
    out["ask"].append(e._ask[i] if i >= 0 else None)
    out["past_bp"].append(_r(s.get("past_bp"), 2))
    out["ofi_ratio"].append(_r(s.get("ofi_ratio"), 4) if s.get("warm") else None)   # 엔진도 워밍업 중엔 판단 안 한다


def _r(x, nd):
    return None if x is None else round(x, nd)


def trade_chart(trade: dict, spec: Spec | None = None, now: float | None = None) -> dict:
    """거래 한 건(web.trades 행)의 상세 차트. 신호 15분 전 ~ 청산 15분 뒤 (지금보다 뒤는 자른다)."""
    from .replay import load_window
    spec = spec or Spec.load()
    p = spec.params()
    t_sig = int(trade["t_sig"])
    t_end = int(trade.get("exit_ts") or (t_sig + p.hold))
    lo = t_sig - BEFORE
    hi = int(min(t_end + AFTER, (now or datetime.now(timezone.utc).timestamp()) - 2))
    utc = lambda x: datetime.fromtimestamp(x, tz=timezone.utc)
    q, t = load_window(trade["symbol"], utc(lo - PAD), utc(hi + 1))
    s = build(spec, trade["symbol"], lo, hi, q, t)
    k = t_sig - lo
    got = {"past_bp": s["past_bp"][k], "ofi_ratio": s["ofi_ratio"][k]} if 0 <= k < len(s["t"]) else {}
    want = {"past_bp": trade.get("past_bp"), "ofi_ratio": trade.get("ofi_ratio")}
    ok = all(got.get(x) is not None and want[x] is not None and abs(got[x] - want[x]) < 1e-2 for x in want)
    return {"symbol": trade["symbol"], "trade_id": trade.get("trade_id"), "lo": lo, "hi": hi, "final": hi >= t_end + AFTER,
            "params": {"move_bp": p.move_bp, "ofi_th": p.ofi_th, "tp_bp": p.tp_bp, "sl_bp": p.sl_bp, "hold": p.hold},
            "check": {"ok": ok, "chart": got, "trade": want}, "n_quotes": len(q), "n_trades": len(t), **s}
