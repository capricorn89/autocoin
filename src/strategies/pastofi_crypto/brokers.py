"""브로커: 페이퍼는 PaperExchange + 실시간 호가 뷰, 라이브는 BinanceFuturesBroker + closePosition 비상 손절.

둘 다 src/execution 의 Broker 인터페이스를 그대로 쓰고, 종목마다 RiskGuard 로 감싼다 (trader 는 모드를 모른다).
"""
from __future__ import annotations

import threading
from collections import deque
from datetime import datetime, timezone

from ...execution.base import OrderRejected
from ...execution.live import BinanceFuturesBroker, _code
from ...execution.market import StaleMarketData
from ...execution.types import BookTop, Order, OrderEvent, OrderType, Side, TradePrint
from ...exchange.rest import BinanceRestError


class LiveMarketView:
    """runner 가 WS 로 받는 5호가·체결을 메모리에 들고 있는 MarketView (페이퍼 체결용).

    book() 은 마지막 5호가를 그대로 준다 — 오래됐다고 예외를 던지지 않는다. 청산은 데이터가 끊겨도 해야 하고,
    진입 쪽 데이터 상태는 trader 의 게이트가 먼저 본다. 한 번도 못 받았으면 StaleMarketData.
    """

    def __init__(self, symbol: str, max_trades: int = 20000):
        self.symbol = symbol
        self._book: BookTop | None = None
        self._trades: deque[TradePrint] = deque(maxlen=max_trades)
        self._lock = threading.Lock()

    def on_book_top(self, rec: dict) -> None:
        ts = datetime.fromtimestamp(int(rec.get("T") or rec["E"]) / 1000, tz=timezone.utc)
        b = BookTop(ts, [(float(p), float(q)) for p, q in rec["bids"]], [(float(p), float(q)) for p, q in rec["asks"]])
        if b.bids and b.asks:
            self._book = b                       # 참조 교체 — 읽는 쪽은 잠금 없이 일관된 스냅샷을 본다

    def on_trade(self, rec: dict) -> None:
        t = TradePrint(datetime.fromtimestamp(int(rec["T"]) / 1000, tz=timezone.utc), int(rec["a"]),
                       float(rec["p"]), float(rec["q"]), bool(rec["m"]))
        with self._lock:
            self._trades.append(t)

    def book(self) -> BookTop:
        if self._book is None:
            raise StaleMarketData(f"{self.symbol} 5호가를 아직 받지 못했다")
        return self._book

    def trades_after(self, agg_id: int | None, since: datetime) -> list[TradePrint]:
        with self._lock:
            ts = list(self._trades)
        if agg_id is None:
            return [t for t in ts if t.ts > since]
        return [t for t in ts if t.agg_id > agg_id and t.ts >= since]

    def last_price(self) -> float:
        with self._lock:
            if self._trades:
                return self._trades[-1].price
        return self.book().mid


class PastofiLiveBroker(BinanceFuturesBroker):
    """비상 손절을 closePosition STOP_MARKET 으로 건다 (수량 없이 그 종목 포지션 전체를 닫는다)."""

    def place_stop_market(self, side: Side, qty: float, stop_price: float, client_id: str) -> Order:
        if client_id in self._orders:
            return self._orders[client_id]
        o = Order(client_id, self.symbol, side, OrderType.STOP_MARKET, qty, stop_price=float(self._px(stop_price)),
                  reduce_only=True, placed_ts=self._clock())
        params = {"algoType": "CONDITIONAL", "symbol": self.symbol, "side": side.value, "type": "STOP_MARKET",
                  "triggerPrice": self._px(stop_price), "workingType": "MARK_PRICE", "closePosition": "true",
                  "priceProtect": "FALSE", "clientAlgoId": client_id}
        try:
            r = self.client.post("/fapi/v1/algoOrder", params)
        except BinanceRestError as e:
            if e.status is not None and e.status < 500:
                return self._reject(o, f"{_code(e)}: {(e.body or '')[:120]}")
            r = self._recover(client_id, algo=True)
        self._track(o)
        self._apply_algo(o, r)
        self._emit(OrderEvent(o.placed_ts, client_id, "placed",
                              {"type": "STOP_MARKET", "stop": o.stop_price, "closePosition": True}))
        return o


__all__ = ["LiveMarketView", "PastofiLiveBroker", "OrderRejected"]
