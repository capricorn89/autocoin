"""실주문 브로커: 바이낸스 USDT-M 선물 (WOO-25). PaperExchange 와 같은 Broker 인터페이스.

실제 자금이 집행된다. make_broker('live', confirm_live=True) 로만 만든다.

엔드포인트 (2026-09 기준)
 - 일반 주문: POST/GET/DELETE /fapi/v1/order, GET /fapi/v1/openOrders, DELETE /fapi/v1/allOpenOrders
 - 조건부 주문(STOP_MARKET): 2025-12-09 부터 Algo 서비스로 이전 — POST/GET/DELETE /fapi/v1/algoOrder,
   GET /fapi/v1/openAlgoOrders, DELETE /fapi/v1/algoOpenOrders. 옛 경로로 보내면 -4120
 - 체결: GET /fapi/v1/userTrades (수수료·maker 여부 포함), 포지션: GET /fapi/v3/positionRisk

중복 주문 방지: 주문 POST 는 자동 재시도하지 않는다. 네트워크 오류면 client_id 로 조회해
접수됐으면 그 주문을 쓰고, 없으면 OrderRejected 를 던져 호출측이 판단하게 한다.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Callable

from ..exchange.auth import BinanceCredentials
from ..exchange.rest import BinanceRestError, FuturesRestClient
from .base import OrderRejected
from .types import (FeeSchedule, Fill, Liquidity, Order, OrderEvent, OrderType, Position, Side,
                    Status)

# 거래소 오류 코드 → 주문 상태로 흡수할 것들 (예외 대신 EXPIRED 주문으로 돌려준다)
_EXPIRE_CODES = {
    -5022: "GTX: 즉시 체결될 가격 (post-only 거절)",
    -2022: "reduceOnly 거절",
    -4118: "reduceOnly 거절 (포지션 부족)",
}
_UNKNOWN_ORDER = {-2011, -2013}
# 계정 쪽 원인이라 코드로 풀 수 없는 거부 — 사람이 조치할 내용을 사유에 붙인다
_ACCOUNT_HINTS = {
    -4411: "TradFi 무기한 약관 미동의 — 바이낸스 웹/앱 선물 화면에서 사용자가 직접 동의해야 한다",
}
_ALGO_STATUS = {"NEW": Status.NEW, "WORKING": Status.NEW, "PARTIALLY_TRIGGERED": Status.NEW,
                "TRIGGERED": Status.FILLED, "FINISHED": Status.FILLED,
                "CANCELED": Status.CANCELED, "EXPIRED": Status.EXPIRED, "REJECTED": Status.REJECTED}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _ms(ts: int) -> datetime:
    return datetime.fromtimestamp(ts / 1000, tz=timezone.utc)


def _code(e: BinanceRestError) -> int | None:
    import json
    try:
        return int(json.loads(e.body or "{}").get("code"))
    except (ValueError, TypeError):
        return None


class BinanceFuturesBroker:
    mode = "live"

    def __init__(self, symbol: str, fees: FeeSchedule | None = None,
                 client: FuturesRestClient | None = None,
                 clock: Callable[[], datetime] = _utc_now):
        self.symbol = symbol
        self.fee_schedule = fees
        self.client = client or FuturesRestClient(credentials=BinanceCredentials.from_env())
        self._clock = clock
        self._orders: dict[str, Order] = {}
        self._by_exchange_id: dict[int, str] = {}
        self._fills: list[Fill] = []
        self._last_trade_id: int | None = None
        self._subs: list[Callable[[OrderEvent], None]] = []
        self._pos_cache = Position(symbol)
        self._load_filters()

    # ------------------------------------------------------------ 준비
    def _load_filters(self) -> None:
        info = self.client.get("/fapi/v1/exchangeInfo")
        sym = next(s for s in info["symbols"] if s["symbol"] == self.symbol)
        f = {x["filterType"]: x for x in sym["filters"]}
        self.tick = float(f["PRICE_FILTER"]["tickSize"])
        self.step = float(f["LOT_SIZE"]["stepSize"])
        self.min_qty = float(f["LOT_SIZE"]["minQty"])

    def verify_account(self, expected: dict) -> list[str]:
        """스펙의 account 블록과 실제 계정 설정 비교. 불일치 목록 (빈 리스트면 정상)."""
        cfg = self.client.get("/fapi/v1/symbolConfig", {"symbol": self.symbol}, signed=True)[0]
        multi = self.client.get("/fapi/v1/multiAssetsMargin", {}, signed=True)["multiAssetsMargin"]
        dual = self.client.get("/fapi/v1/positionSide/dual", {}, signed=True)["dualSidePosition"]
        got = {"margin_type": cfg["marginType"], "leverage": int(cfg["leverage"]),
               "multi_assets_margin": bool(multi), "dual_side_position": bool(dual)}
        bad = [f"{k}: 기대 {expected[k]!r}, 실제 {got[k]!r}" for k in got if k in expected and expected[k] != got[k]]
        if "fees_bps" in expected:
            fr = self.client.get("/fapi/v1/commissionRate", {"symbol": self.symbol}, signed=True)
            maker, taker = float(fr["makerCommissionRate"]) * 1e4, float(fr["takerCommissionRate"]) * 1e4
            exp = expected["fees_bps"]
            if abs(maker - exp["maker"]) > 1e-6 or abs(taker - exp["taker"]) > 1e-6:
                bad.append(f"fees_bps: 기대 {exp}, 실제 maker {maker:g} / taker {taker:g}")
        return bad

    def _qty(self, qty: float) -> str:
        steps = round(qty / self.step)
        if steps * self.step < self.min_qty:
            raise OrderRejected(f"수량 {qty} 가 최소 {self.min_qty} 미만")
        return f"{steps * self.step:.{max(0, -int(math.floor(math.log10(self.step))))}f}"

    def _px(self, price: float) -> str:
        ticks = round(price / self.tick)
        return f"{ticks * self.tick:.{max(0, -int(math.floor(math.log10(self.tick))))}f}"

    # ------------------------------------------------------------ 주문
    def place_market(self, side: Side, qty: float, client_id: str, reduce_only: bool = False) -> Order:
        return self._place(Order(client_id, self.symbol, side, OrderType.MARKET, qty, reduce_only=reduce_only),
                           {"type": "MARKET", "newOrderRespType": "RESULT"})

    def place_limit_gtx(self, side: Side, qty: float, price: float, client_id: str,
                        reduce_only: bool = False) -> Order:
        px = self._px(price)                     # 거래소에 보내는 틱 단위 값을 주문에도 기록
        o = Order(client_id, self.symbol, side, OrderType.LIMIT, qty, price=float(px), reduce_only=reduce_only)
        return self._place(o, {"type": "LIMIT", "timeInForce": "GTX", "price": px})

    def place_stop_market(self, side: Side, qty: float, stop_price: float, client_id: str) -> Order:
        """서버측 비상 손절. Algo 주문, 표시가(MARK_PRICE) 기준, reduceOnly. priceProtect 는 끈다
        (보호 조건 때문에 급변 시 발동이 막히면 비상 손절의 의미가 없다)."""
        if client_id in self._orders:
            return self._orders[client_id]
        o = Order(client_id, self.symbol, side, OrderType.STOP_MARKET, qty, stop_price=float(self._px(stop_price)),
                  reduce_only=True, placed_ts=self._clock())
        params = {"algoType": "CONDITIONAL", "symbol": self.symbol, "side": side.value, "type": "STOP_MARKET",
                  "quantity": self._qty(qty), "triggerPrice": self._px(stop_price), "workingType": "MARK_PRICE",
                  "reduceOnly": "true", "priceProtect": "FALSE", "clientAlgoId": client_id}
        try:
            r = self.client.post("/fapi/v1/algoOrder", params)
        except BinanceRestError as e:
            if e.status is not None and e.status < 500:
                return self._reject(o, f"{_code(e)}: {(e.body or '')[:120]}")
            r = self._recover(client_id, algo=True)
        self._track(o)
        self._apply_algo(o, r)
        self._emit(OrderEvent(o.placed_ts, client_id, "placed", {"type": "STOP_MARKET", "stop": stop_price}))
        return o

    def cancel(self, client_id: str) -> Order:
        o = self.get_order(client_id)
        try:
            if o.type is OrderType.STOP_MARKET:
                r = self.client.delete("/fapi/v1/algoOrder", {"clientAlgoId": client_id})
                o.status = Status.CANCELED if r.get("code") in (200, "200", None) else o.status
            else:
                r = self.client.delete("/fapi/v1/order", {"symbol": self.symbol, "origClientOrderId": client_id})
                self._apply(o, r)
        except BinanceRestError as e:
            if _code(e) in _UNKNOWN_ORDER:
                raise OrderRejected(f"{client_id}: 취소할 주문 없음 ({_code(e)})") from e
            raise
        o.updated_ts = self._clock()
        self._emit(OrderEvent(o.updated_ts, client_id, "canceled", {}))
        return o

    def cancel_all(self) -> list[Order]:
        before = self.open_orders()
        self.client.delete("/fapi/v1/allOpenOrders", {"symbol": self.symbol})
        self.client.delete("/fapi/v1/algoOpenOrders", {"symbol": self.symbol})
        for o in before:
            o.status, o.updated_ts = Status.CANCELED, self._clock()
            self._emit(OrderEvent(o.updated_ts, o.client_id, "canceled", {"via": "cancel_all"}))
        return before

    # ------------------------------------------------------------ 조회
    def get_order(self, client_id: str) -> Order:
        if client_id not in self._orders:
            raise KeyError(client_id)
        return self._orders[client_id]

    def open_orders(self) -> list[Order]:
        """거래소 기준 미체결 (우리가 추적하지 않던 주문도 포함 — 재시작 복구용)."""
        out = []
        for r in self.client.get("/fapi/v1/openOrders", {"symbol": self.symbol}, signed=True):
            o = self._orders.get(r["clientOrderId"]) or self._track(Order(
                r["clientOrderId"], self.symbol, Side(r["side"]), OrderType(r["type"]), float(r["origQty"]),
                price=float(r["price"]) or None, reduce_only=bool(r["reduceOnly"])))
            self._apply(o, r)
            out.append(o)
        for r in self.client.get("/fapi/v1/openAlgoOrders", {"symbol": self.symbol}, signed=True):
            cid = r["clientAlgoId"]
            o = self._orders.get(cid) or self._track(Order(
                cid, self.symbol, Side(r["side"]), OrderType.STOP_MARKET, float(r["quantity"]),
                stop_price=float(r["triggerPrice"]), reduce_only=True))
            self._apply_algo(o, r)
            out.append(o)
        return [o for o in out if o.status.is_open]

    def position(self) -> Position:
        rows = self.client.get("/fapi/v3/positionRisk", {"symbol": self.symbol}, signed=True)
        amt = sum(float(r["positionAmt"]) for r in rows)
        entry = next((float(r["entryPrice"]) for r in rows if float(r["positionAmt"]) != 0), 0.0)
        self._pos_cache.qty, self._pos_cache.entry_price = amt, entry
        return self._pos_cache

    def fills(self) -> list[Fill]:
        return list(self._fills)

    def subscribe(self, fn: Callable[[OrderEvent], None]) -> None:
        self._subs.append(fn)

    def poll(self) -> list[OrderEvent]:
        """추적 중인 미체결 주문 상태 갱신 + 새 체결(userTrades) 반영."""
        events: list[OrderEvent] = []
        self._subs.append(events.append)
        try:
            for o in [o for o in self._orders.values() if o.status.is_open]:
                prev = o.status
                if o.type is OrderType.STOP_MARKET:
                    self._apply_algo(o, self.client.get("/fapi/v1/algoOrder", {"clientAlgoId": o.client_id}, signed=True))
                    if o.status is Status.FILLED and prev.is_open:
                        self._emit(OrderEvent(self._clock(), o.client_id, "triggered", {}))
                else:
                    self._apply(o, self.client.get("/fapi/v1/order", {"symbol": self.symbol,
                                                                       "origClientOrderId": o.client_id}, signed=True))
                    if not o.status.is_open and o.status is not Status.FILLED:
                        self._emit(OrderEvent(self._clock(), o.client_id, o.status.value.lower(), {"reason": o.reason}))
            self._pull_trades()
        finally:
            self._subs.remove(events.append)
        return events

    # ------------------------------------------------------------ 내부
    def _place(self, o: Order, extra: dict) -> Order:
        if o.client_id in self._orders:
            return self._orders[o.client_id]
        o.placed_ts = o.updated_ts = self._clock()
        params = {"symbol": self.symbol, "side": o.side.value, "quantity": self._qty(o.qty),
                  "newClientOrderId": o.client_id, **extra}
        if o.reduce_only:
            params["reduceOnly"] = "true"
        try:
            r = self.client.post("/fapi/v1/order", params)
        except BinanceRestError as e:
            code = _code(e)
            if code in _EXPIRE_CODES:
                self._track(o)
                o.status, o.reason = Status.EXPIRED, _EXPIRE_CODES[code]
                self._emit(OrderEvent(o.updated_ts, o.client_id, "expired", {"reason": o.reason}))
                return o
            if e.status is not None and e.status < 500:
                return self._reject(o, f"{code}: {(e.body or '')[:120]}")
            r = self._recover(o.client_id, algo=False)
        self._track(o)
        self._apply(o, r)
        self._emit(OrderEvent(o.placed_ts, o.client_id, "placed",
                              {"type": o.type.value, "side": o.side.value, "qty": o.qty, "price": o.price}))
        if o.type is OrderType.MARKET:
            self._pull_trades()
        return o

    def _recover(self, client_id: str, algo: bool) -> dict:
        """POST 가 네트워크 오류·5xx 로 끝났을 때 실제 접수 여부 확인."""
        try:
            if algo:
                return self.client.get("/fapi/v1/algoOrder", {"clientAlgoId": client_id}, signed=True)
            return self.client.get("/fapi/v1/order", {"symbol": self.symbol, "origClientOrderId": client_id},
                                   signed=True)
        except BinanceRestError as e:
            if _code(e) in _UNKNOWN_ORDER:
                raise OrderRejected(f"{client_id}: 전송 실패, 거래소에 없음 — 재시도 여부는 호출측 판단") from e
            raise

    def _reject(self, o: Order, reason: str) -> Order:
        code = next((c for c in _ACCOUNT_HINTS if reason.startswith(f"{c}:")), None)
        if code is not None:
            reason = f"{code}: {_ACCOUNT_HINTS[code]}"
        self._track(o)
        o.status, o.reason = Status.REJECTED, reason
        self._emit(OrderEvent(self._clock(), o.client_id, "rejected", {"reason": reason}))
        raise OrderRejected(f"{o.client_id}: {reason}")

    def _track(self, o: Order) -> Order:
        self._orders[o.client_id] = o
        return o

    def _apply(self, o: Order, r: dict) -> None:
        o.exchange_id = int(r["orderId"])
        self._by_exchange_id[o.exchange_id] = o.client_id
        o.status = Status(r["status"])
        o.filled_qty = float(r.get("executedQty", 0) or 0)
        o.avg_price = float(r.get("avgPrice", 0) or 0)
        if "updateTime" in r:
            o.updated_ts = _ms(int(r["updateTime"]))

    def _apply_algo(self, o: Order, r: dict) -> None:
        o.exchange_id = int(r["algoId"])
        o.status = _ALGO_STATUS.get(r.get("algoStatus", "NEW"), Status.NEW)
        actual = r.get("actualOrderId")
        if actual:
            self._by_exchange_id[int(actual)] = o.client_id
            o.avg_price = float(r.get("actualPrice") or 0) or o.avg_price
            o.filled_qty = float(r.get("actualQty") or 0) or o.filled_qty
        if "updateTime" in r:
            o.updated_ts = _ms(int(r["updateTime"]))

    def _pull_trades(self) -> None:
        params = {"symbol": self.symbol, "limit": 1000}
        if self._last_trade_id is not None:
            params["fromId"] = self._last_trade_id + 1
        else:
            since = min((o.placed_ts for o in self._orders.values() if o.placed_ts), default=self._clock())
            params["startTime"] = int(since.timestamp() * 1000) - 1000
        for t in self.client.get("/fapi/v1/userTrades", params, signed=True):
            self._last_trade_id = int(t["id"])
            cid = self._by_exchange_id.get(int(t["orderId"]), f"order:{t['orderId']}")
            fee = float(t["commission"]) if t.get("commissionAsset", "USDT") == "USDT" else 0.0
            f = Fill(cid, _ms(int(t["time"])), Side(t["side"]), float(t["qty"]), float(t["price"]), fee,
                     Liquidity.MAKER if t["maker"] else Liquidity.TAKER)
            self._fills.append(f)
            self._pos_cache.realized_pnl += float(t.get("realizedPnl", 0) or 0)
            self._pos_cache.fees += fee
            self._emit(OrderEvent(f.ts, cid, "filled", {"qty": f.qty, "price": f.price, "fee": fee,
                                                        "liquidity": f.liquidity.value}))

    def _emit(self, ev: OrderEvent) -> None:
        for fn in list(self._subs):
            fn(ev)


def smoke(argv: list[str] | None = None) -> int:
    """실주문 경로 점검 (사용자가 직접 실행). 기본은 체결 없는 주문만:
      1) 계정 설정 = 스펙 확인  2) 1호가 -2% GTX 매수 0.03 → 대기 확인 → 취소
      3) Algo 손절 주문 → 대기 확인 → 취소  4) 미체결 0 확인
    --round-trip 이면 5) 시장가 매수 0.03 → reduceOnly 시장가 매도 (수수료 약 $0.005)
    """
    import argparse
    import time
    from ..overnight.spec import Spec
    ap = argparse.ArgumentParser(prog="python -m src.execution.live")
    ap.add_argument("--confirm-live", action="store_true", help="실제 주문을 낸다는 확인 (필수)")
    ap.add_argument("--round-trip", action="store_true", help="시장가 왕복 체결까지 (0.03 EWY)")
    args = ap.parse_args(argv)
    if not args.confirm_live:
        print("실제 주문을 냅니다. --confirm-live 를 붙여 다시 실행하세요.")
        return 2
    spec = Spec.load()
    b = BinanceFuturesBroker(spec.raw["symbol"])
    b.subscribe(lambda e: print(f"  {e.ts:%H:%M:%S} {e.client_id:12s} {e.kind:9s} {e.detail}"))
    tag = datetime.now(timezone.utc).strftime("%H%M%S")
    ok = True

    def step(name, cond):
        nonlocal ok
        ok &= bool(cond)
        print(f"{'✅' if cond else '❌'} {name}")

    bad = b.verify_account(spec.raw["account"])
    step(f"계정 설정 = 스펙 {bad or ''}", not bad)
    if b.position().qty != 0 or b.open_orders():
        print("포지션이나 미체결이 있어 중단합니다."); return 1
    bid = float(b.client.get("/fapi/v1/ticker/bookTicker", {"symbol": b.symbol})["bidPrice"])

    def run(name, fn):
        """단계 실행. 거부되면 ❌ 로 표시하고 False (예외로 죽지 않고 정리 단계까지 간다)."""
        try:
            fn()
            return True
        except OrderRejected as e:
            step(f"{name} 거부: {e}", False)
            return False

    def limit_leg():
        o = b.place_limit_gtx(Side.BUY, 0.03, bid * 0.98, f"smk-l-{tag}")
        time.sleep(2); b.poll()
        step(f"GTX 매수 대기 ({o.status.value})", o.status is Status.NEW)
        b.cancel(o.client_id); b.poll()
        step(f"GTX 취소 ({o.status.value})", o.status is Status.CANCELED)

    def stop_leg():
        s = b.place_stop_market(Side.SELL, 0.03, bid * 0.88, f"smk-s-{tag}")
        time.sleep(2); b.poll()
        step(f"Algo 손절 대기 ({s.status.value})", s.status is Status.NEW)
        b.cancel(s.client_id)
        step("Algo 손절 취소", s.status is Status.CANCELED)

    def round_trip():
        e = b.place_market(Side.BUY, 0.03, f"smk-e-{tag}")
        step(f"시장가 매수 ({e.status.value} @ {e.avg_price})", e.status is Status.FILLED)
        x = b.place_market(Side.SELL, 0.03, f"smk-x-{tag}", reduce_only=True)
        step(f"reduceOnly 시장가 매도 ({x.status.value} @ {x.avg_price})", x.status is Status.FILLED)
        b.poll()
        step(f"체결 {len(b.fills())}건, 수수료 {sum(f.fee for f in b.fills()):.4f} USDT", len(b.fills()) >= 2)

    try:
        if run("GTX 매수", limit_leg):          # 첫 주문이 계정 사유로 거부되면 나머지도 같은 이유라 건너뛴다
            run("Algo 손절", stop_leg)
            if args.round_trip:
                run("시장가 왕복", round_trip)
    finally:
        if b.open_orders():
            b.cancel_all()
        pos = b.position().qty
        if pos:
            b.place_market(Side.SELL if pos > 0 else Side.BUY, abs(pos), f"smk-flat-{tag}", reduce_only=True)
        step("미체결 0 · 포지션 0", not b.open_orders() and b.position().qty == 0)
    print("통과" if ok else "실패 항목 있음")
    return 0 if ok else 1


if __name__ == "__main__":
    import sys
    sys.exit(smoke())
