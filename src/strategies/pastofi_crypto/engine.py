"""종목 하나의 실시간 엔진 — 주식 1호가·체결로 신호, 근월물 선물 1호가로 가상 체결 (1.1~1.3).

맥 백테스트(phase3_signal_test/pastreturn_ofi_daily.py)와 판정이 같다는 걸 tests/test_golden.py가
실데이터로 고정한다. 표준 라이브러리만 쓴다.

입력
    on_quote(ts, bid1, bidq1, ask1, askq1, continuous, in_session)
        ts          : 수신 시각, 하루 시작부터의 초(실수, KST). 맥은 LS 수신 시각을 썼다.
        continuous  : 접속매매 호가면 True. 단일가(장전·VI·장마감)면 False.
        in_session  : 거래소 호가시각이 09:00:00 이상 15:20:00 미만이면 True.
        - continuous=False 프레임은 'OFI 끊김' 표시만 남긴다(다음 호가와 차분하지 않는다).
          in_session이면 단일가(VI) 시각으로 기록한다.
        - continuous=True 인데 in_session=False면 버린다(끊김 표시도 안 함).
        - 직전에 받아들인 호가와 1호가 4개 값이 모두 같으면 버린다.
    on_trade(sec, qty, side)
        sec  : 거래소 체결 시각(정수 초, 하루 시작부터). side: +1 매수, -1 매도, 0 미상(안 셈).
    on_fut_quote(ts, bid1, bidq1, ask1, askq1)   (1.1, 잔량은 1.3부터 — 인자 순서가 바뀌었다)
        그 종목 근월물 주식선물의 최우선 호가와 잔량(계약). ts는 주식 호가와 같은 시계(수신 시각).
        양쪽 호가: 매수1 > 0 이고 매도1 > 매수1. 한쪽 호가(1.2): 매수1·매도1 중 하나만 0 초과 —
        가격제한(상·하한가)에 잠긴 상태다. 둘 다 0이거나 매도1 ≤ 매수1(둘 다 > 0)이면 버린다.
    advance(T)
        정수 초 T까지 판단·시간 청산을 진행한다. 부르기 전에 ts ≤ T 인 호가와
        sec ≤ T-1 인 체결을 모두 넣어라. 늦게 들어온 호가도 시각으로 찾아 쓰지만,
        T 판단에 쓰일 호가가 advance(T) 뒤에 오면 그건 반영되지 않는다.
    finish()
        장 끝. 남은 포지션을 정리한다.

판단 (초 T마다, T-1초까지 데이터만)
    c = T-1
    past  = (ln mid(c) - ln mid(c-300)) × 1e4                  mid(x) = x 이하 마지막 호가의 (매수1+매도1)/2
    OFI   = Σ e  (호가 시각이 (c-60, c] 인 스냅샷)                e = 1호가 OFI (Cont·Kukanov·Stoikov 2014)
    거래량 = Σ qty (체결 초가 (c-660, c-60], side≠0) / 10        → OFI 비율 = OFI / 분당 거래량
    로직1: past ≥ +200 & OFI 비율 < -0.5 → 매도 / 로직2: past ≤ -200 & OFI 비율 > +0.5 → 매수
    판단 안 함: T가 [09:10, 15:15) 밖 · c-660이 09:00 전 · (c-721, c] 안에 단일가 프레임 ·
              그 로직 포지션 보유 중 · 직전 청산 시각 > T
    필터로 건너뛴 신호가 이어지는 중 (1.3): 그 로직이 필터로 건너뛴 마지막 신호초와 60초 안에 다시 신호가 나면
              무시하고 그 초를 '마지막 신호초'로 삼는다 (필터를 통과해도, 아래 진입 조건을 따지기 전에).
진입·청산 (선물 호가 체결 가정, 1.1)
    진입: T 이하 마지막 **양쪽 선물** 호가 (매도 → 매수1, 매수 → 매도1). 선물 호가가 없으면 진입 안 함.
          그 호가 mid가 선물 기준가 대비 ±9.5% 이상이거나 기준가를 모르면 진입 안 함 (1.2, 가격제한 근접)
          T 이하 마지막 주식 호가 시각부터 T까지 주식 단일가 프레임이 있어도 진입 안 함 (VI 중)
          선물 호가 필터 (1.3): 그 진입 선물 호가의 진입 쪽 잔량(매도 → 매수1 잔량, 매수 → 매도1 잔량) < 2계약이거나
          (선물 매도1 − 매수1) − (주식 매도1 − 매수1) ≥ 2 × 호가단위(T 이하 마지막 주식 호가 mid, tick.py)면
          진입 안 함 → 건너뛴 신호로 기록(skipped)하고 위의 '이어지는 중' 규칙이 시작된다
    익절·손절 (1.3): 진입 선물 호가 뒤 선물 호가(한쪽 호가 포함, 청산 쪽 가격이 있는 것)마다 차례로
          매도: 매도1 ≤ 진입가×0.98 → 익절, 매도1 ≥ 진입가×1.01 → 손절
          매수: 매수1 ≥ 진입가×1.02 → 익절, 매수1 ≤ 진입가×0.99 → 손절 — 먼저 닿은 호가로
    시간: T+300 이하 마지막 선물 호가 (매도는 매도1, 매수는 매수1). 그 호가에 청산 쪽 가격이 없으면(잠김)
          청산 쪽 가격이 있는 다음 첫 호가 (1.2, 사유 '5분(가격제한 뒤)')
"""
from __future__ import annotations

import math
from bisect import bisect_left, bisect_right
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional

from .params import DEFAULT, Params
from .tick import MARKETS, tick_size

LOGIC1, LOGIC2 = "로직1", "로직2"
_SIDE = {LOGIC1: -1, LOGIC2: +1}
_KEEP_SEC = 900        # 이보다 오래된 호가·체결은 지운다(판단에 필요한 건 최대 721초 전)


@dataclass
class Trade:
    logic: str
    side: int              # -1 매도 / +1 매수
    t_sig: int             # 판단 초 T
    t_in: float            # 진입 호가 시각
    p_in: float
    t_out: float
    p_out: float
    reason: str            # 익절 / 손절 / 5분 / 5분(가격제한 뒤)
    past_bp: float
    ofi1m: float
    vol_per_min: float
    ofi_ratio: float
    mid_start: float       # mid(c-300)
    mid_cut: float         # mid(c)
    pnl_bp: float          # 호가 체결 기준, 비용 전
    net_bp: float          # 비용 후
    version: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class _Pos:
    logic: str
    side: int
    t_sig: int
    t_in: float
    p_in: float
    deadline: int
    feat: dict
    wait: bool = False     # 5분 마감 때 청산 쪽 호가가 없어(가격제한) 풀리기를 기다리는 중 (1.2)


class SymbolEngine:
    def __init__(self, params: Params = DEFAULT, fut_base: Optional[float] = None, market: str = "") -> None:
        """fut_base: 그 종목 근월물 선물의 그날 기준가(전일 정산가). 없으면 진입하지 않는다 (1.2).
        market: 그 종목 시장 'KOSPI' / 'KOSDAQ' — 호가단위(1.3 필터)에 쓴다. 없으면 만들지 않는다."""
        if str(market).upper() not in MARKETS:
            raise ValueError(f"market은 KOSPI 또는 KOSDAQ여야 한다 (받은 값: {market!r}) — 1.3 호가 필터의 호가단위에 쓴다")
        self.p = params
        self.fut_base = float(fut_base) if fut_base else None
        self.market = str(market).upper()
        # 받아들인 접속매매 호가 (시각순)
        self._ts: List[float] = []
        self._bid: List[float] = []
        self._ask: List[float] = []
        self._mid: List[float] = []
        self._cum: List[float] = []        # e 누적합 (그 호가까지)
        self._last_raw: Optional[tuple] = None
        self._last_q: Optional[tuple] = None   # (bid, bq, ask, aq) — 잔량은 가격 0이면 0
        self._brk = True                   # 직전 받아들인 호가 뒤에 단일가 프레임이 있었나
        self._cum_e = 0.0
        self._auct: List[float] = []       # 장중 단일가 프레임 시각
        self._fts: List[float] = []        # 선물 최우선 호가 (1.1)
        self._fbid: List[float] = []
        self._fask: List[float] = []
        self._fbq: List[float] = []        # 양쪽 선물 호가의 매수1·매도1 잔량 (1.3)
        self._faq: List[float] = []
        self._xts: List[float] = []        # 청산 판정용: 양쪽 + 한쪽 선물 호가 전부 (1.2, 없는 쪽은 0)
        self._xbid: List[float] = []
        self._xask: List[float] = []
        self._vsec: List[int] = []         # 체결이 있었던 초 (오름차순)
        self._vcum: List[int] = []         # 그 초까지 방향 있는 체결량 누적
        self._pos: Dict[str, Optional[_Pos]] = {LOGIC1: None, LOGIC2: None}
        self._free: Dict[str, float] = {LOGIC1: -1e18, LOGIC2: -1e18}
        self._last_T = params.entry_start - 1
        self.trades: List[Trade] = []
        self.n_quotes = 0
        self.n_dup = 0
        self.n_no_fut = 0      # 신호는 났는데 선물 호가가 없어 진입 못 한 횟수
        self.n_fut_limit = 0   # 신호는 났는데 선물이 기준가 ±9.5% 이상(또는 기준가 모름)이라 건너뛴 초 수 (1.2)
        self._last_skip: Dict[str, float] = {LOGIC1: -1e18, LOGIC2: -1e18}   # (1.3) 필터로 건너뛴 마지막 신호초
        self.skipped: List[dict] = []   # (1.3) 선물 호가 필터로 건너뛴 신호 (한 건 = 이어진 신호 묶음)
        self.n_chain = 0       # (1.3) 건너뛴 신호가 이어지는 중이라 무시한 신호초 수

    # ── 입력 ────────────────────────────────────────────────────────────────

    def on_trade(self, sec: int, qty: int, side: int) -> None:
        if side == 0 or qty <= 0:
            return
        sec, qty = int(sec), int(qty)
        if self._vsec and sec == self._vsec[-1]:
            self._vcum[-1] += qty
        elif not self._vsec or sec > self._vsec[-1]:
            self._vsec.append(sec)
            self._vcum.append((self._vcum[-1] if self._vcum else 0) + qty)
        else:                                  # 늦게 온 체결: 끼워 넣고 뒤 누적을 고친다
            i = bisect_left(self._vsec, sec)
            if self._vsec[i] != sec:
                self._vsec.insert(i, sec)
                self._vcum.insert(i, self._vcum[i - 1] if i else 0)
            for j in range(i, len(self._vcum)):
                self._vcum[j] += qty

    def _vol_upto(self, x: int) -> int:
        i = bisect_right(self._vsec, x) - 1
        return self._vcum[i] if i >= 0 else 0

    def on_quote(self, ts: float, bid1, bidq1, ask1, askq1, continuous: bool, in_session: bool) -> None:
        if not continuous:
            self._brk = True
            if in_session:
                self._auct.append(float(ts))
            return
        if not in_session:
            return
        raw = (bid1, bidq1, ask1, askq1)
        if self._last_raw is not None and raw == self._last_raw:
            self.n_dup += 1
            return
        self._last_raw = raw
        bq = bidq1 if bid1 > 0 else 0
        aq = askq1 if ask1 > 0 else 0
        if self._last_q is None or self._brk:
            e = 0.0
        else:
            b0, bq0, a0, aq0 = self._last_q
            ofb = bq if bid1 > b0 else (bq - bq0 if bid1 == b0 else -bq0)
            ofa = -aq0 if ask1 > a0 else (aq - aq0 if ask1 == a0 else aq)
            e = float(ofb - ofa)
        self._last_q = (bid1, bq, ask1, aq)
        self._brk = False
        self._cum_e += e
        ts = float(ts)
        self._ts.append(ts)
        self._bid.append(float(bid1))
        self._ask.append(float(ask1))
        self._mid.append((bid1 + ask1) / 2.0)
        self._cum.append(self._cum_e)
        self.n_quotes += 1

    def on_fut_quote(self, ts: float, bid1, bidq1, ask1, askq1) -> None:
        ts = float(ts)
        two = bid1 > 0 and ask1 > bid1
        if not two and (bid1 > 0) == (ask1 > 0):   # 둘 다 없거나 둘 다 있는데 뒤집힘 → 버린다
            return
        if two:
            self._fts.append(ts)
            self._fbid.append(float(bid1))
            self._fask.append(float(ask1))
            self._fbq.append(float(bidq1))
            self._faq.append(float(askq1))
        self._xts.append(ts)
        self._xbid.append(float(max(bid1, 0)))
        self._xask.append(float(max(ask1, 0)))
        for lg, pos in list(self._pos.items()):
            if pos is not None:
                self._check_exit(pos, len(self._xts) - 1)

    # ── 시계 ────────────────────────────────────────────────────────────────

    def advance(self, T: int) -> None:
        for s in range(self._last_T + 1, int(T) + 1):
            self._second(s)
        self._last_T = max(self._last_T, int(T))

    def finish(self) -> None:
        self.advance(self.p.session_close)
        for lg, pos in list(self._pos.items()):
            if pos is None:
                continue
            if pos.wait:                          # 장 끝까지 가격제한이 안 풀렸다 → 청산 쪽 가격이 있던 마지막 호가
                px = self._xask if pos.side < 0 else self._xbid
                i = max((k for k in range(len(px)) if px[k] > 0), default=-1)
                self._close(pos, i, "5분(가격제한, 장끝)")
            else:                                 # 정상이면 advance에서 이미 닫혔다
                self._time_exit(pos, pos.deadline)

    # ── 내부 ────────────────────────────────────────────────────────────────

    def _idx(self, x: float) -> int:
        """x 이하 마지막 호가 인덱스 (없으면 -1)."""
        return bisect_right(self._ts, x) - 1

    def _fidx(self, x: float) -> int:
        """x 이하 마지막 양쪽 선물 호가 인덱스 (없으면 -1)."""
        return bisect_right(self._fts, x) - 1

    def _xidx(self, x: float) -> int:
        """x 이하 마지막 선물 호가(한쪽 포함) 인덱스 (없으면 -1)."""
        return bisect_right(self._xts, x) - 1

    def _near_limit(self, i: int) -> bool:
        """양쪽 선물 호가 i의 mid가 기준가 대비 ±fut_limit_bp 이상인가 (기준가를 모르면 True)."""
        if not self.fut_base:
            return True
        m, lim = (self._fbid[i] + self._fask[i]) / 2.0, self.p.fut_limit_bp / 1e4
        return m >= self.fut_base * (1 + lim) or m <= self.fut_base * (1 - lim)

    def _auct_in(self, lo: float, hi: float, lo_closed: bool = False) -> bool:
        """단일가 프레임이 (lo, hi] (lo_closed면 [lo, hi]) 안에 있었나."""
        a = bisect_left(self._auct, lo) if lo_closed else bisect_right(self._auct, lo)
        return bisect_right(self._auct, hi) - a > 0

    def _check_exit(self, pos: _Pos, i: int) -> None:
        """선물 호가 i(_x 배열)로 익절 판정. 5분 마감 뒤 기다리는 중이면 청산 쪽 가격이 나온 첫 호가로 청산."""
        t = self._xts[i]
        px = self._xask[i] if pos.side < 0 else self._xbid[i]
        if t <= pos.t_in or px <= 0:
            return
        if pos.wait:
            self._close(pos, i, "5분(가격제한 뒤)")
            return
        if t > pos.deadline:
            return
        tp, sl = self.p.tp_bp / 1e4, self.p.sl_bp / 1e4
        if (pos.side < 0 and px <= pos.p_in * (1 - tp)) or (pos.side > 0 and px >= pos.p_in * (1 + tp)):
            self._close(pos, i, "익절")
        elif (pos.side < 0 and px >= pos.p_in * (1 + sl)) or (pos.side > 0 and px <= pos.p_in * (1 - sl)):
            self._close(pos, i, "손절")

    def _time_exit(self, pos: _Pos, s: int) -> None:
        """5분 마감: s 이하 마지막 선물 호가. 청산 쪽 가격이 없으면(가격제한) 풀리기를 기다린다."""
        i = self._xidx(s)
        px = (self._xask[i] if pos.side < 0 else self._xbid[i]) if i >= 0 else 0
        if px > 0:
            self._close(pos, i, "5분")
        else:
            pos.wait = True

    def _close(self, pos: _Pos, i: int, reason: str) -> None:
        """선물 호가 i(_x 배열)로 청산."""
        px = self._xask[i] if pos.side < 0 else self._xbid[i]
        pnl = pos.side * math.log(px / pos.p_in) * 1e4
        self.trades.append(Trade(
            logic=pos.logic, side=pos.side, t_sig=pos.t_sig, t_in=pos.t_in, p_in=pos.p_in,
            t_out=self._xts[i], p_out=px, reason=reason, pnl_bp=pnl, net_bp=pnl - self.p.cost_bp,
            version=self.p.version, **pos.feat))
        self._pos[pos.logic] = None
        self._free[pos.logic] = self._xts[i]

    def _features(self, T: int) -> Optional[dict]:
        p = self.p
        c = T - p.guard
        lo_vol = c - p.ofi_win - p.vol_win
        if lo_vol < p.session_open or c - p.move_win < p.session_open:
            return None
        if self._auct_in(lo_vol - p.vi_pad, c, lo_closed=True):
            return None
        ia, ib = self._idx(c - p.move_win), self._idx(c)
        if ia < 0 or ib < 0:
            return None
        past = (math.log(self._mid[ib]) - math.log(self._mid[ia])) * 1e4
        if abs(past) < p.move_bp:              # 어느 로직도 안 걸린다 (계산 절약, 판정은 같다)
            return None
        io = self._idx(c - p.ofi_win)
        ofi = self._cum[ib] - (self._cum[io] if io >= 0 else 0.0)
        vol = self._vol_upto(c - p.ofi_win) - self._vol_upto(lo_vol)
        per_min = vol / (p.vol_win / 60.0)
        if not per_min > 0:
            return None
        return dict(past_bp=past, ofi1m=ofi, vol_per_min=per_min, ofi_ratio=ofi / per_min,
                    mid_start=self._mid[ia], mid_cut=self._mid[ib])

    def _second(self, s: int) -> None:
        p = self.p
        # 1) 시간 청산 (마감 = 진입 판단 초 + hold) — 마감 이하 마지막 선물 호가
        for lg, pos in list(self._pos.items()):
            if pos is None or pos.deadline != s or pos.wait:
                continue
            self._time_exit(pos, s)
        # 2) 진입 판단
        if not (p.entry_start <= s < p.entry_end) or s + p.hold > p.session_close:
            self._prune(s)
            return
        f = None
        for lg in (LOGIC1, LOGIC2):
            if self._pos[lg] is not None or s < self._free[lg]:
                continue
            if f is None:
                f = self._features(s)
                if f is None:
                    break
            r, past = f["ofi_ratio"], f["past_bp"]
            hit = (past >= p.move_bp and r < -p.ofi_th) if lg == LOGIC1 else \
                  (past <= -p.move_bp and r > p.ofi_th)
            if not hit:
                continue
            if s - self._last_skip[lg] <= p.skip_chain:      # 필터로 건너뛴 신호가 이어지는 중 (1.3)
                self._last_skip[lg] = s
                self.n_chain += 1
                ep = self.skipped[-1]
                ep["end"], ep["n_sec"] = s, ep["n_sec"] + 1
                continue
            i0 = self._idx(s)
            if i0 < 0 or self._auct_in(self._ts[i0], s):   # 주식이 단일가(VI) 중이면 진입 안 함
                continue
            f0 = self._fidx(s)
            if f0 < 0:                                        # 선물 호가가 없으면 진입 못 한다
                self.n_no_fut += 1
                continue
            if self._near_limit(f0):                          # 가격제한 근접이면 매매 안 함 (1.2)
                self.n_fut_limit += 1
                continue
            side = _SIDE[lg]
            rem = self._fbq[f0] if side < 0 else self._faq[f0]  # 선물 호가 필터 (1.3)
            tsz = tick_size(self._mid[i0], self.market)
            gap = ((self._fask[f0] - self._fbid[f0]) - (self._ask[i0] - self._bid[i0])) / tsz
            if rem < p.fut_min_rem or gap >= p.fut_gap_ticks:
                self._last_skip[lg] = s
                self.skipped.append(dict(
                    logic=lg, t_sig=s, end=s, n_sec=1, fut_rem=rem, fut_bid=self._fbid[f0], fut_ask=self._fask[f0],
                    stk_bid=self._bid[i0], stk_ask=self._ask[i0], tick=tsz, gap_ticks=gap,
                    why="+".join(x for x, c in (("잔량", rem < p.fut_min_rem), ("갭", gap >= p.fut_gap_ticks)) if c),
                    version=p.version, **f))
                continue
            pos = _Pos(logic=lg, side=side, t_sig=s, t_in=self._fts[f0],
                       p_in=self._fbid[f0] if side < 0 else self._fask[f0],
                       deadline=s + p.hold, feat=f)
            self._pos[lg] = pos
            for i in range(bisect_right(self._xts, pos.t_in), len(self._xts)):   # 판단보다 먼저 들어와 있던 뒤 선물 호가
                if self._pos[lg] is None or self._xts[i] > pos.deadline:
                    break
                self._check_exit(pos, i)
        self._prune(s)

    def _prune(self, s: int) -> None:
        if s % 60:
            return
        cut = s - _KEEP_SEC
        k = bisect_right(self._ts, cut) - 1        # cut 이하 마지막 하나는 남긴다
        if k > 0 and all(pos is None for pos in self._pos.values()):
            for a in (self._ts, self._bid, self._ask, self._mid, self._cum):
                del a[:k]
        kf = bisect_right(self._fts, cut) - 1
        if kf > 0 and all(pos is None for pos in self._pos.values()):
            for a in (self._fts, self._fbid, self._fask, self._fbq, self._faq):
                del a[:kf]
        kx = bisect_right(self._xts, cut) - 1
        if kx > 0 and all(pos is None for pos in self._pos.values()):
            for a in (self._xts, self._xbid, self._xask):
                del a[:kx]
        ka = bisect_right(self._auct, cut - 100)
        if ka > 0:
            del self._auct[:ka]
        kv = bisect_right(self._vsec, cut - 100) - 1   # 누적값이라 기준점 하나는 남긴다
        if kv > 0:
            del self._vsec[:kv]
            del self._vcum[:kv]
