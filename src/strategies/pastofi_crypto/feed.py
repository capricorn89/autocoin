"""WS 레코드 → 엔진. 실시간 실행(runner)과 DB 재생(replay)이 이 클래스 하나를 같이 쓴다.

입력 레코드는 수집기(src/exchange/ws.py OrderBookCollector)가 싱크에 쓰는 모양 그대로다.
    book_top : {"kind": "book_top", "symbol", "T", "E", "bids": [[p, q], ...], "asks": [...]}
    trade    : {"kind": "trade", "symbol", "T", "q", "m", ...}   (aggTrade 원본)

엔진 입력 규칙 (research/pastofi_crypto/run_all.py 와 같다)
    - 시계는 거래소 시각. 호가 = book_top T(없으면 E), 체결 = aggTrade T. UTC 자정부터의 초. UTC 날짜마다 엔진 하나
    - 호가: 그날 첫 레코드 + 1호가 4개 값(bid1, bidq1, ask1, askq1)이 직전 레코드와 다를 때만.
      on_quote(continuous=True, in_session=True) 와 on_fut_quote 에 같은 값을 넣는다 (신호·체결이 같은 계약)
    - 잔량·체결량 ×qty_scale(1000). 엔진 on_trade 가 체결량을 int 로 자르기 때문 (비율은 그대로다)
    - 체결: (초, 방향)별 수량 합을 모아 두었다가 tick(T) 직전에 초 ≤ T-1 인 것만 round(합 × 1000) 으로 넣는다
    - tick(T) = advance(T). 실시간은 거래소 시각 T+decision_delay 에 부른다. 그 사이 들어온 (T, T+0.3] 호가가
      먼저 들어가 있어도 판단 결과는 같다 (엔진은 T 이하 호가만 쓰고, 뒤 호가는 진입 뒤 청산 판정에 시각순으로 쓴다)
    - finish_day: 그날 마지막 tick 뒤 engine.finish(). 초 86399 체결은 넣지 않는다 (run_all.py 와 같다)

엔진은 로직1·2를 다 돌린다 (복사본을 고치지 않으려고). 로직1 판정은 로직2 상태와 무관하다.
로직1 진입·청산만 콜백으로 알린다. 이 콜백은 '엔진이 그렇게 판단했다'는 뜻이고, 실제로 주문할지는 trader 가 정한다
(위험 한도·데이터 상태로 건너뛰어도 엔진은 백테스트와 똑같이 계속 돈다 → 매일 대조가 거래 단위로 맞는다).
"""
from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Callable, Optional

from .engine import LOGIC1, SymbolEngine, Trade
from .spec import Spec

DAY = 86400


@dataclass
class Signal:
    """엔진 로직1 진입 판단."""
    symbol: str
    day: int                 # UTC 날짜 (1970-01-01 부터의 일수)
    t_sig: int               # 판단 초 T (그날 자정부터)
    t_in: float              # 진입 가정 호가 시각
    p_in: float              # 진입 가정가 (T 이하 마지막 bid1)
    deadline: int            # t_sig + hold
    feat: dict

    @property
    def epoch_sig(self) -> int:
        return self.day * DAY + self.t_sig


@dataclass
class Exit:
    """엔진 로직1 청산 판단."""
    symbol: str
    day: int
    trade: Trade

    @property
    def t_sig(self) -> int:
        return self.trade.t_sig


@dataclass
class _DayState:
    engine: SymbolEngine
    last_l1: Optional[tuple] = None
    n_trades: int = 0
    pos_tsig: Optional[int] = None
    last_T: int = -1
    trades: dict = field(default_factory=lambda: defaultdict(float))   # (sec, side) → 수량 합 (아직 안 넣은 것)
    late_trades: int = 0     # 이미 지난 초의 체결이 늦게 온 건수 (진단용)


class SymbolFeed:
    def __init__(self, spec: Spec, symbol: str,
                 on_entry: Callable[[Signal], None] = lambda s: None,
                 on_exit: Callable[[Exit], None] = lambda e: None):
        self.spec = spec
        self.symbol = symbol
        self.scale = spec.qty_scale
        self.logic = spec.logic
        self.on_entry = on_entry
        self.on_exit = on_exit
        self.days: dict[int, _DayState] = {}
        self.finished: set[int] = set()
        self.pending_trades: dict[int, dict] = defaultdict(lambda: defaultdict(float))   # 엔진 생기기 전 체결
        self.l1: Optional[tuple] = None          # 마지막 1호가 (bid, bidq, ask, askq), 원 수량
        self.l1_ts: Optional[float] = None       # 그 거래소 시각 (epoch 초)

    # ------------------------------------------------------------ 입력
    def on_book_top(self, rec: dict) -> None:
        ts_ms = rec.get("T") or rec["E"]
        b, bq = (float(x) for x in rec["bids"][0])
        a, aq = (float(x) for x in rec["asks"][0])
        self.on_l1(int(ts_ms), b, bq, a, aq)

    def on_l1(self, ts_ms: int, b: float, bq: float, a: float, aq: float) -> None:
        day, ms = divmod(int(ts_ms), DAY * 1000)
        if day in self.finished:
            return
        l1 = (b, bq, a, aq)
        self.l1, self.l1_ts = l1, ts_ms / 1000
        st = self.days.get(day)
        if st is None:
            st = self.days[day] = _DayState(self.spec.new_engine((b + a) / 2.0))
            for k, q in self.pending_trades.pop(day, {}).items():
                st.trades[k] += q
        elif st.last_l1 == l1:
            return
        st.last_l1 = l1
        t = ms / 1000.0
        S = self.scale
        st.engine.on_quote(t, b, bq * S, a, aq * S, True, True)
        st.engine.on_fut_quote(t, b, bq * S, a, aq * S)
        self._emit(day, st)

    def on_trade(self, rec: dict) -> None:
        self.on_trade_raw(int(rec["T"]), float(rec["q"]), bool(rec["m"]))

    def on_trade_raw(self, ts_ms: int, qty: float, buyer_maker: bool) -> None:
        sec_all = int(ts_ms) // 1000
        day, sec = divmod(sec_all, DAY)
        if day in self.finished:
            return
        side = -1 if buyer_maker else 1
        st = self.days.get(day)
        if st is None:
            self.pending_trades[day][(sec, side)] += qty
            return
        if sec <= st.last_T - 1:
            st.late_trades += 1
        st.trades[(sec, side)] += qty

    # ------------------------------------------------------------ 시계
    def tick(self, day: int, T: int) -> None:
        """그날 초 T 판단. 초 ≤ T-1 체결을 넣고 advance(T)."""
        st = self.days.get(day)
        if st is None or day in self.finished:
            return
        if st.trades:
            ready = sorted(k for k in st.trades if k[0] <= T - 1)
            for k in ready:
                q = st.trades.pop(k)
                st.engine.on_trade(k[0], int(round(q * self.scale)), k[1])
        st.engine.advance(T)
        st.last_T = max(st.last_T, T)
        self._emit(day, st)

    def finish_day(self, day: int) -> None:
        st = self.days.get(day)
        self.finished.add(day)
        self.pending_trades.pop(day, None)
        if st is None:
            return
        st.engine.finish()
        self._emit(day, st)
        del self.days[day]

    # ------------------------------------------------------------ 조회
    def engine(self, day: int) -> Optional[SymbolEngine]:
        st = self.days.get(day)
        return st.engine if st else None

    def late_trades(self, day: int) -> int:
        st = self.days.get(day)
        return st.late_trades if st else 0

    def snapshot(self, day: int, T: int) -> dict:
        """웹 표시용: 초 T 기준 past bp · OFI 비율 (문턱과 무관하게 계산). 엔진 판정과 같은 창을 쓴다."""
        st = self.days.get(day)
        if st is None:
            return {}
        e, p = st.engine, st.engine.p
        c = T - p.guard
        out: dict = {"warm": c - p.ofi_win - p.vol_win >= p.session_open}
        ia, ib = e._idx(c - p.move_win), e._idx(c)
        if ia >= 0 and ib >= 0:
            out["past_bp"] = (math.log(e._mid[ib]) - math.log(e._mid[ia])) * 1e4
            io = e._idx(c - p.ofi_win)
            ofi = e._cum[ib] - (e._cum[io] if io >= 0 else 0.0)
            vol = e._vol_upto(c - p.ofi_win) - e._vol_upto(c - p.ofi_win - p.vol_win)
            per_min = vol / (p.vol_win / 60.0)
            out["ofi1m"] = ofi / self.scale
            out["vol_per_min"] = per_min / self.scale
            out["ofi_ratio"] = ofi / per_min if per_min > 0 else None
        return out

    # ------------------------------------------------------------ 내부
    def _emit(self, day: int, st: _DayState) -> None:
        e = st.engine
        if len(e.trades) > st.n_trades:
            new = e.trades[st.n_trades:]
            st.n_trades = len(e.trades)
            for tr in new:
                if tr.logic != self.logic:
                    continue
                if st.pos_tsig != tr.t_sig:          # 같은 호출 안에서 진입하고 바로 청산됐다
                    self.on_entry(Signal(self.symbol, day, tr.t_sig, tr.t_in, tr.p_in,
                                         tr.t_sig + e.p.hold, _feat(tr)))
                st.pos_tsig = None
                self.on_exit(Exit(self.symbol, day, tr))
        pos = e._pos[self.logic]
        if pos is not None and pos.t_sig != st.pos_tsig:
            st.pos_tsig = pos.t_sig
            self.on_entry(Signal(self.symbol, day, pos.t_sig, pos.t_in, pos.p_in, pos.deadline, dict(pos.feat)))


def _feat(tr: Trade) -> dict:
    return dict(past_bp=tr.past_bp, ofi1m=tr.ofi1m, vol_per_min=tr.vol_per_min, ofi_ratio=tr.ofi_ratio,
                mid_start=tr.mid_start, mid_cut=tr.mid_cut)
