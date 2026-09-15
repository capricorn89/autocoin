"""로컬 오더북 + 시퀀스 검증 (Binance USDT-M diff depth).

동기화 규칙 (Binance Futures 문서 "How to manage a local order book correctly"):
 1. diff 스트림을 먼저 구독하고 이벤트를 버퍼링한다.
 2. REST 스냅샷을 받는다 (lastUpdateId).
 3. u < lastUpdateId 인 이벤트는 버린다 (스냅샷에 이미 반영됨).
 4. 첫 적용 이벤트는 U <= lastUpdateId <= u 여야 한다.
 5. 이후 이벤트는 pu == 직전 이벤트의 u 여야 한다. 아니면 갭 → 재동기화.
 6. 수량은 증분이 아니라 절대값이며, 수량 0 은 해당 가격 레벨 삭제.

한계: 스냅샷은 limit(최대 1000) 레벨만 담는다. 그 밖의 레벨은 diff 로만 채워지므로
      깊은 호가는 불완전할 수 있다. 상위 N 레벨 기반 시그널에는 문제없다.
"""
from __future__ import annotations

import heapq


class SequenceGapError(RuntimeError):
    """diff 이벤트 시퀀스가 끊김 — 로컬 오더북을 신뢰할 수 없음."""


class LocalOrderBook:
    def __init__(self, symbol: str):
        self.symbol = symbol
        self.bids: dict[float, float] = {}
        self.asks: dict[float, float] = {}
        self.last_update_id: int | None = None
        self.last_event_time: int | None = None
        self._first_applied = False

    def reset(self) -> None:
        self.bids.clear()
        self.asks.clear()
        self.last_update_id = None
        self.last_event_time = None
        self._first_applied = False

    @property
    def ready(self) -> bool:
        """스냅샷 이후 최소 1개 diff 가 연속성 검증을 통과했는지."""
        return self.last_update_id is not None and self._first_applied

    def apply_snapshot(self, snapshot: dict) -> None:
        self.reset()
        for p, q in snapshot["bids"]:
            if float(q) > 0:
                self.bids[float(p)] = float(q)
        for p, q in snapshot["asks"]:
            if float(q) > 0:
                self.asks[float(p)] = float(q)
        self.last_update_id = int(snapshot["lastUpdateId"])
        self.last_event_time = snapshot.get("E")

    def apply_diff(self, event: dict) -> bool:
        """diff 적용. 스냅샷보다 오래된 이벤트면 False(폐기), 갭이면 SequenceGapError."""
        if self.last_update_id is None:
            raise RuntimeError(f"{self.symbol}: 스냅샷 적용 전에는 diff 를 적용할 수 없음")
        U, u, pu = int(event["U"]), int(event["u"]), int(event["pu"])

        if not self._first_applied:
            if u < self.last_update_id:
                return False
            if U > self.last_update_id:
                raise SequenceGapError(
                    f"{self.symbol}: 스냅샷 lastUpdateId={self.last_update_id} 가 "
                    f"첫 이벤트 U={U} 보다 오래됨")
        elif pu != self.last_update_id:
            raise SequenceGapError(
                f"{self.symbol}: pu={pu} != 직전 u={self.last_update_id} (U={U}, u={u})")

        self._apply_levels(self.bids, event.get("b", []))
        self._apply_levels(self.asks, event.get("a", []))
        self.last_update_id = u
        self.last_event_time = event.get("E")
        self._first_applied = True
        return True

    @staticmethod
    def _apply_levels(side: dict[float, float], levels) -> None:
        for p, q in levels:
            price, qty = float(p), float(q)
            if qty == 0.0:
                side.pop(price, None)
            else:
                side[price] = qty

    # ---- 조회 ----
    def best_bid(self) -> tuple[float, float] | None:
        if not self.bids:
            return None
        p = max(self.bids)
        return p, self.bids[p]

    def best_ask(self) -> tuple[float, float] | None:
        if not self.asks:
            return None
        p = min(self.asks)
        return p, self.asks[p]

    def top(self, n: int = 10) -> tuple[list[tuple[float, float]], list[tuple[float, float]]]:
        # 100ms 마다 호출되므로 전체 정렬 대신 O(레벨수) 부분 선택
        bids = heapq.nlargest(n, self.bids.items(), key=lambda x: x[0])
        asks = heapq.nsmallest(n, self.asks.items(), key=lambda x: x[0])
        return bids, asks

    def is_crossed(self) -> bool:
        """최우선 매수호가 >= 최우선 매도호가 — 오더북 손상 신호."""
        bb, ba = self.best_bid(), self.best_ask()
        return bb is not None and ba is not None and bb[0] >= ba[0]
