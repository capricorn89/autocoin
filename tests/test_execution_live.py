"""실주문 브로커 (WOO-25) — 가짜 REST 로 요청 형태·오류 처리만 검증. 네트워크 없음."""
import json
from datetime import datetime, timezone

import pytest

from src.exchange.rest import BinanceRestError
from src.execution.base import OrderRejected
from src.execution.live import BinanceFuturesBroker
from src.execution.types import Liquidity, OrderType, Side, Status

T0 = datetime(2026, 10, 1, 6, 30, tzinfo=timezone.utc)
INFO = {"symbols": [{"symbol": "EWYUSDT", "filters": [
    {"filterType": "PRICE_FILTER", "tickSize": "0.01000"},
    {"filterType": "LOT_SIZE", "stepSize": "0.01", "minQty": "0.01"}]}]}


def err(status, code, msg="x"):
    return BinanceRestError(f"HTTP {status}", status, json.dumps({"code": code, "msg": msg}))


class FakeRest:
    def __init__(self):
        self.calls = []
        self.responses = {}          # (method, path) -> list of responses/exceptions (FIFO)

    def on(self, method, path, *resps):
        self.responses.setdefault((method, path), []).extend(resps)

    def _next(self, method, path, params):
        self.calls.append((method, path, dict(params or {})))
        q = self.responses.get((method, path))
        if not q:
            if path == "/fapi/v1/exchangeInfo":
                return INFO
            if path == "/fapi/v1/userTrades":
                return []
            raise AssertionError(f"응답 없음: {method} {path}")
        r = q.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def get(self, path, params=None, signed=False):
        return self._next("GET", path, params)

    def post(self, path, params=None, signed=True):
        return self._next("POST", path, params)

    def delete(self, path, params=None, signed=True):
        return self._next("DELETE", path, params)


def order_resp(cid, status="NEW", qty="1.00", price="186.00", executed="0", avg="0", oid=11, type_="LIMIT", side="BUY"):
    return {"orderId": oid, "clientOrderId": cid, "status": status, "origQty": qty, "price": price,
            "executedQty": executed, "avgPrice": avg, "type": type_, "side": side, "reduceOnly": False,
            "updateTime": 1790000000000}


@pytest.fixture
def br():
    rest = FakeRest()
    return BinanceFuturesBroker("EWYUSDT", client=rest, clock=lambda: T0), rest


def posted(rest, path):
    return [p for m, pth, p in rest.calls if m == "POST" and pth == path]


def test_market_order_params_and_fill(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", order_resp("e1", "FILLED", executed="1.00", avg="186.03", type_="MARKET"))
    rest.on("GET", "/fapi/v1/userTrades", [{"id": 5, "orderId": 11, "time": 1790000000100, "side": "BUY",
                                            "qty": "1.00", "price": "186.03", "commission": "0.0744",
                                            "commissionAsset": "USDT", "maker": False, "realizedPnl": "0"}])
    o = b.place_market(Side.BUY, 1.0, "e1")
    p = posted(rest, "/fapi/v1/order")[0]
    assert p == {"symbol": "EWYUSDT", "side": "BUY", "quantity": "1.00", "newClientOrderId": "e1",
                 "type": "MARKET", "newOrderRespType": "RESULT"}
    assert o.status is Status.FILLED and o.avg_price == 186.03
    f = b.fills()[0]
    assert f.client_id == "e1" and f.liquidity is Liquidity.TAKER and f.fee == pytest.approx(0.0744)


def test_gtx_params_and_post_only_rejection_becomes_expired(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", err(400, -5022))
    o = b.place_limit_gtx(Side.SELL, 1.0, 186.004, "x1", reduce_only=True)
    p = posted(rest, "/fapi/v1/order")[0]
    assert p["timeInForce"] == "GTX" and p["price"] == "186.00" and p["reduceOnly"] == "true"
    assert o.status is Status.EXPIRED and "GTX" in o.reason


def test_reduce_only_rejection_becomes_expired(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", err(400, -2022))
    assert b.place_market(Side.SELL, 1.0, "x1", reduce_only=True).status is Status.EXPIRED


def test_other_client_error_raises(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", err(400, -1111, "Precision is over the maximum"))
    with pytest.raises(OrderRejected):
        b.place_market(Side.BUY, 1.0, "e1")
    assert b.get_order("e1").status is Status.REJECTED


def test_network_error_recovers_by_client_id_without_resending(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", BinanceRestError("timeout"))
    rest.on("GET", "/fapi/v1/order", order_resp("b1", "NEW"))
    o = b.place_limit_gtx(Side.BUY, 1.0, 186.0, "b1")
    assert o.status is Status.NEW and o.exchange_id == 11
    assert len(posted(rest, "/fapi/v1/order")) == 1          # 재전송하지 않음


def test_network_error_and_not_on_exchange_raises(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", BinanceRestError("timeout"))
    rest.on("GET", "/fapi/v1/order", err(400, -2013, "Order does not exist."))
    with pytest.raises(OrderRejected, match="거래소에 없음"):
        b.place_market(Side.BUY, 1.0, "e1")


def test_quantity_below_min_is_refused_before_sending(br):
    b, rest = br
    with pytest.raises(OrderRejected):
        b.place_market(Side.BUY, 0.001, "e1")
    assert posted(rest, "/fapi/v1/order") == []


def test_stop_uses_algo_endpoint(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/algoOrder", {"algoId": 77, "clientAlgoId": "sl", "algoStatus": "NEW"})
    o = b.place_stop_market(Side.SELL, 1.0, 163.555, "sl")
    p = posted(rest, "/fapi/v1/algoOrder")[0]
    assert p == {"algoType": "CONDITIONAL", "symbol": "EWYUSDT", "side": "SELL", "type": "STOP_MARKET",
                 "quantity": "1.00", "triggerPrice": "163.56", "workingType": "MARK_PRICE",
                 "reduceOnly": "true", "priceProtect": "FALSE", "clientAlgoId": "sl"}
    assert posted(rest, "/fapi/v1/order") == []
    assert o.type is OrderType.STOP_MARKET and o.status is Status.NEW


def test_cancel_stop_goes_to_algo_endpoint(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/algoOrder", {"algoId": 77, "clientAlgoId": "sl", "algoStatus": "NEW"})
    rest.on("DELETE", "/fapi/v1/algoOrder", {"code": "200", "msg": "success"})
    b.place_stop_market(Side.SELL, 1.0, 163.5, "sl")
    assert b.cancel("sl").status is Status.CANCELED
    assert rest.calls[-1][:2] == ("DELETE", "/fapi/v1/algoOrder")


def test_cancel_unknown_raises(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", order_resp("b1"))
    rest.on("DELETE", "/fapi/v1/order", err(400, -2011, "Unknown order sent."))
    b.place_limit_gtx(Side.BUY, 1.0, 186.0, "b1")
    with pytest.raises(OrderRejected):
        b.cancel("b1")


def test_cancel_all_hits_both_endpoints(br):
    b, rest = br
    rest.on("GET", "/fapi/v1/openOrders", [order_resp("b1")])
    rest.on("GET", "/fapi/v1/openAlgoOrders", [{"algoId": 77, "clientAlgoId": "sl", "algoStatus": "NEW",
                                                "side": "SELL", "quantity": "1.00", "triggerPrice": "160"}])
    rest.on("DELETE", "/fapi/v1/allOpenOrders", {"code": 200})
    rest.on("DELETE", "/fapi/v1/algoOpenOrders", {"code": 200})
    out = b.cancel_all()
    assert {o.client_id for o in out} == {"b1", "sl"}
    assert all(o.status is Status.CANCELED for o in out)


def test_poll_picks_up_gtx_fill_as_maker(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", order_resp("b1"))
    b.place_limit_gtx(Side.BUY, 1.0, 186.0, "b1")
    rest.on("GET", "/fapi/v1/order", order_resp("b1", "FILLED", executed="1.00", avg="186.00"))
    rest.on("GET", "/fapi/v1/userTrades", [{"id": 9, "orderId": 11, "time": 1790000000500, "side": "BUY",
                                            "qty": "1.00", "price": "186.00", "commission": "0",
                                            "commissionAsset": "USDT", "maker": True}])
    evs = b.poll()
    assert b.get_order("b1").status is Status.FILLED
    assert [e.kind for e in evs] == ["filled"] and b.fills()[0].liquidity is Liquidity.MAKER


def test_verify_account_reports_mismatch(br):
    b, rest = br
    rest.on("GET", "/fapi/v1/symbolConfig", [{"marginType": "CROSSED", "leverage": 20}])
    rest.on("GET", "/fapi/v1/multiAssetsMargin", {"multiAssetsMargin": False})
    rest.on("GET", "/fapi/v1/positionSide/dual", {"dualSidePosition": False})
    rest.on("GET", "/fapi/v1/commissionRate", {"makerCommissionRate": "0", "takerCommissionRate": "0.000400"})
    bad = b.verify_account({"margin_type": "ISOLATED", "leverage": 1, "multi_assets_margin": False,
                            "dual_side_position": False, "fees_bps": {"maker": 0.0, "taker": 4.0}})
    assert len(bad) == 2 and "margin_type" in bad[0] and "leverage" in bad[1]


def test_tradfi_agreement_missing_gives_actionable_reason(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", err(400, -4411, "Please sign TradFi-Perps agreement contract fapi."))
    with pytest.raises(OrderRejected, match="약관 미동의"):
        b.place_limit_gtx(Side.BUY, 0.03, 180.0, "b1")
    assert "약관" in b.get_order("b1").reason


def test_order_records_tick_rounded_prices(br):
    b, rest = br
    rest.on("POST", "/fapi/v1/order", order_resp("b1"))
    rest.on("POST", "/fapi/v1/algoOrder", {"algoId": 77, "clientAlgoId": "sl", "algoStatus": "NEW"})
    assert b.place_limit_gtx(Side.BUY, 0.03, 182.2506, "b1").price == 182.25
    assert b.place_stop_market(Side.SELL, 0.03, 163.6536, "sl").stop_price == 163.65
