"""페이퍼 거래소: 실시간 5호가·체결로 주문을 시뮬 체결한다 (WOO-24).

체결 모델 (가정 — 결정 노트와 같이 유지)
 - MARKET: 반대편 5호가를 위에서부터 먹어 VWAP 체결, taker 수수료. 5호가를 넘는 수량은 최악 호가로 채운다
 - LIMIT GTX(post-only): 넣는 순간 반대편 최우선 호가에 닿으면(즉시 체결될 가격이면) EXPIRED.
   대기 중에는 **체결이 우리 가격을 뚫고 지나가야** 체결로 본다 (매수: 체결가 < 지정가).
   같은 가격 체결은 대기열 순서를 몰라 체결로 치지 않고 touched 로만 기록 (보수적). maker 수수료
 - STOP_MARKET: 최근 체결가가 손절가에 닿으면(매도: ≤) 발동 → 그 순간 5호가로 MARKET 체결.
   실거래소는 표시가(mark) 기준이지만 DB 에 mark 가 없어 최근 체결가로 대신한다
 - reduceOnly: 포지션을 늘리거나 부호를 바꾸는 주문은 거부. 대기 주문은 체결 시점에 다시 검사해 EXPIRED
 - 같은 client_id 로 다시 넣으면 새 주문을 만들지 않고 기존 주문을 돌려준다 (재시도 멱등성)
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .base import OrderRejected
from .market import MarketView
from .types import (BookTop, FeeSchedule, Fill, Liquidity, Order, OrderEvent, OrderType, Position,
                    Side, Status, TradePrint)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PaperExchange:
    mode = "paper"

    def __init__(self, symbol: str, market: MarketView, fees: FeeSchedule,
                 clock: Callable[[], datetime] = _utc_now, state_file: str | Path | None = None):
        self.symbol = symbol
        self.market = market
        self.fee_schedule = fees
        self._clock = clock
        self._orders: dict[str, Order] = {}
        self._fills: list[Fill] = []
        self._pos = Position(symbol)
        self._touched: dict[str, datetime] = {}          # GTX 가 같은 가격 체결을 처음 본 시각
        self._last_agg: int | None = None
        self._last_ts: datetime | None = None
        self._subs: list[Callable[[OrderEvent], None]] = []
        self._state_file = Path(state_file) if state_file else None
        if self._state_file and self._state_file.exists():
            self._load()

    # ------------------------------------------------------------ 조회
    def get_order(self, client_id: str) -> Order:
        if client_id not in self._orders:
            raise KeyError(client_id)
        return self._orders[client_id]

    def open_orders(self) -> list[Order]:
        return [o for o in self._orders.values() if o.status.is_open]

    def position(self) -> Position:
        return self._pos

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def fills_since(self, since: datetime) -> list[Fill]:
        return [f for f in self._fills if f.ts >= since]

    def touched_at(self, client_id: str) -> datetime | None:
        return self._touched.get(client_id)

    def subscribe(self, fn: Callable[[OrderEvent], None]) -> None:
        self._subs.append(fn)

    # ------------------------------------------------------------ 주문
    def place_market(self, side: Side, qty: float, client_id: str, reduce_only: bool = False) -> Order:
        if client_id in self._orders:
            return self._orders[client_id]
        o = self._new(client_id, side, OrderType.MARKET, qty, reduce_only=reduce_only)
        if not self._reduce_ok(o):
            return self._close(o, Status.EXPIRED, "reduceOnly: 포지션을 줄이지 않는 주문")
        self._fill_market(o, self.market.book())
        return o

    def place_limit_gtx(self, side: Side, qty: float, price: float, client_id: str,
                        reduce_only: bool = False) -> Order:
        if client_id in self._orders:
            return self._orders[client_id]
        o = self._new(client_id, side, OrderType.LIMIT, qty, price=price, reduce_only=reduce_only)
        if not self._reduce_ok(o):
            return self._close(o, Status.EXPIRED, "reduceOnly: 포지션을 줄이지 않는 주문")
        book = self.market.book()
        crosses = price >= book.best_ask if side is Side.BUY else price <= book.best_bid
        if crosses:
            return self._close(o, Status.EXPIRED, "GTX: 즉시 체결될 가격")
        self._reset_cursor(o)
        self._save()
        return o

    def place_stop_market(self, side: Side, qty: float, stop_price: float, client_id: str) -> Order:
        if client_id in self._orders:
            return self._orders[client_id]
        o = self._new(client_id, side, OrderType.STOP_MARKET, qty, stop_price=stop_price, reduce_only=True)
        last = self.market.last_price()
        if (side is Side.SELL and last <= stop_price) or (side is Side.BUY and last >= stop_price):
            return self._close(o, Status.REJECTED, "손절가가 이미 지남 (Order would immediately trigger)")
        self._reset_cursor(o)
        self._save()
        return o

    def cancel(self, client_id: str) -> Order:
        o = self.get_order(client_id)
        if not o.status.is_open:
            raise OrderRejected(f"{client_id}: 이미 {o.status.value} (Unknown order sent)")
        return self._close(o, Status.CANCELED, "", kind="canceled")

    def cancel_all(self) -> list[Order]:
        return [self.cancel(o.client_id) for o in self.open_orders()]

    # ------------------------------------------------------------ 진행
    def poll(self) -> list[OrderEvent]:
        """새 체결을 읽어 대기 GTX·손절을 진행한다."""
        events: list[OrderEvent] = []
        self._subs.append(events.append)
        try:
            resting = self.open_orders()
            if not resting:
                return events
            since = self._last_ts or min(o.placed_ts for o in resting)
            for t in self.market.trades_after(self._last_agg, since):
                self._last_agg, self._last_ts = t.agg_id, t.ts
                for o in [o for o in self.open_orders() if o.placed_ts < t.ts]:
                    if o.type is OrderType.LIMIT:
                        self._maybe_fill_limit(o, t)
                    elif o.type is OrderType.STOP_MARKET:
                        self._maybe_trigger_stop(o, t)
        finally:
            self._subs.remove(events.append)
        self._save()
        return events

    # ------------------------------------------------------------ 내부
    def _new(self, client_id, side, type_, qty, price=None, stop_price=None, reduce_only=False) -> Order:
        if qty <= 0:
            raise OrderRejected(f"{client_id}: 수량 {qty}")
        now = self._clock()
        o = Order(client_id=client_id, symbol=self.symbol, side=side, type=type_, qty=qty, price=price,
                  stop_price=stop_price, reduce_only=reduce_only, placed_ts=now, updated_ts=now)
        self._orders[client_id] = o
        self._emit(OrderEvent(now, client_id, "placed", {"type": type_.value, "side": side.value,
                                                          "qty": qty, "price": price, "stop": stop_price}))
        return o

    def _reduce_ok(self, o: Order) -> bool:
        if not o.reduce_only:
            return True
        pos = self._pos.qty
        return pos != 0 and (pos > 0) != (o.side is Side.BUY) and o.remaining <= abs(pos) + 1e-9

    def _close(self, o: Order, status: Status, reason: str, kind: str | None = None) -> Order:
        o.status, o.reason, o.updated_ts = status, reason, self._clock()
        self._emit(OrderEvent(o.updated_ts, o.client_id, kind or status.value.lower(), {"reason": reason}))
        self._save()
        return o

    def _fill_market(self, o: Order, book: BookTop) -> None:
        levels = book.asks if o.side is Side.BUY else book.bids
        left, cost = o.remaining, 0.0
        for px, q in levels:
            take = min(left, q)
            cost += take * px
            left -= take
            if left <= 1e-12:
                break
        if left > 1e-12:
            cost += left * levels[-1][0]                     # 5호가 초과분은 최악 호가로
        self._record_fill(o, o.remaining, cost / o.remaining, Liquidity.TAKER, self._clock())

    def _maybe_fill_limit(self, o: Order, t: TradePrint) -> None:
        through = t.price < o.price if o.side is Side.BUY else t.price > o.price
        if t.price == o.price and o.client_id not in self._touched:
            self._touched[o.client_id] = t.ts
        if not through:
            return
        if not self._reduce_ok(o):
            self._close(o, Status.EXPIRED, "reduceOnly: 체결 시점에 포지션 없음")
            return
        self._record_fill(o, o.remaining, o.price, Liquidity.MAKER, t.ts)

    def _maybe_trigger_stop(self, o: Order, t: TradePrint) -> None:
        hit = t.price <= o.stop_price if o.side is Side.SELL else t.price >= o.stop_price
        if not hit:
            return
        self._emit(OrderEvent(t.ts, o.client_id, "triggered", {"trade_price": t.price}))
        if not self._reduce_ok(o):
            self._close(o, Status.EXPIRED, "reduceOnly: 발동 시점에 포지션 없음")
            return
        self._fill_market(o, self.market.book())

    def _record_fill(self, o: Order, qty: float, price: float, liq: Liquidity, ts: datetime) -> None:
        fee = self.fee_schedule.fee(qty * price, liq)
        self._pos.apply(o.side, qty, price, fee)
        f = Fill(o.client_id, ts, o.side, qty, price, fee, liq)
        self._fills.append(f)
        o.avg_price = (o.avg_price * o.filled_qty + price * qty) / (o.filled_qty + qty)
        o.filled_qty = round(o.filled_qty + qty, 8)
        o.status = Status.FILLED if o.remaining <= 1e-9 else Status.PARTIALLY_FILLED
        o.updated_ts = ts
        self._emit(OrderEvent(ts, o.client_id, "filled", {"qty": qty, "price": price, "fee": fee,
                                                          "liquidity": liq.value}))
        self._save()

    def _reset_cursor(self, o: Order) -> None:
        """첫 대기 주문이 생기면 커서를 그 시각으로. 이전 체결은 poll 에서 placed_ts 로도 걸러진다."""
        if len(self.open_orders()) == 1:
            self._last_agg, self._last_ts = None, o.placed_ts

    def _emit(self, ev: OrderEvent) -> None:
        for fn in list(self._subs):
            fn(ev)

    # ------------------------------------------------------------ 상태 저장 (재시작 복구)
    def _save(self) -> None:
        if not self._state_file:
            return
        state = {"orders": [o.to_dict() for o in self._orders.values()],
                 "fills": [{**f.__dict__, "ts": f.ts.isoformat(), "side": f.side.value,
                            "liquidity": f.liquidity.value} for f in self._fills],
                 "position": self._pos.__dict__,
                 "cursor": {"agg": self._last_agg, "ts": self._last_ts.isoformat() if self._last_ts else None}}
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1))
        tmp.replace(self._state_file)

    def _load(self) -> None:
        s = json.loads(self._state_file.read_text())
        self._orders = {d["client_id"]: Order.from_dict(d) for d in s["orders"]}
        self._fills = [Fill(**{**f, "ts": datetime.fromisoformat(f["ts"]), "side": Side(f["side"]),
                               "liquidity": Liquidity(f["liquidity"])}) for f in s["fills"]]
        self._pos = Position(**s["position"])
        c = s["cursor"]
        self._last_agg = c["agg"]
        self._last_ts = datetime.fromisoformat(c["ts"]) if c["ts"] else None


def smoke(symbol: str = "EWYUSDT", wait_s: int = 60) -> None:
    """실시간 DB 데이터로 페이퍼 왕복 1회 (실주문 없음): GTX 매수 → 대기 → 체결되면 시장가 청산, 아니면 취소."""
    import time
    from .market import DbMarketView
    mv = DbMarketView(symbol)
    ex = PaperExchange(symbol, mv, FeeSchedule(maker_bps=0.0, taker_bps=4.0))
    ex.subscribe(lambda e: print(f"{e.ts:%H:%M:%S.%f} {e.client_id:6s} {e.kind:9s} {e.detail}"))
    book = mv.book()
    print(f"호가 {book.best_bid} / {book.best_ask} (데이터 {book.ts:%H:%M:%S})")
    o = ex.place_limit_gtx(Side.BUY, 1.0, book.best_bid, "smoke-e")
    deadline = time.time() + wait_s
    while o.status.is_open and time.time() < deadline:
        time.sleep(1)
        ex.poll()
    if o.status is Status.FILLED:
        ex.place_market(Side.SELL, 1.0, "smoke-x", reduce_only=True)
    elif o.status.is_open:
        print(f"{wait_s}s 동안 미체결 (touched={ex.touched_at('smoke-e')}) → 취소")
        ex.cancel("smoke-e")
    p = ex.position()
    print(f"포지션 {p.qty} 실현 {p.realized_pnl:+.4f} 수수료 {p.fees:.4f}")
    mv.close()


if __name__ == "__main__":
    smoke()
