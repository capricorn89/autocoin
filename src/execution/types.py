"""주문·체결·포지션 자료형. 페이퍼(paper.py)와 실주문(live.py)이 같이 쓴다.

값과 상태 이름은 바이낸스 USDT-M 선물 API 를 따른다 (side, type, status, timeInForce).
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime
from enum import Enum


class Side(str, Enum):
    BUY = "BUY"
    SELL = "SELL"

    @property
    def sign(self) -> int:
        return 1 if self is Side.BUY else -1


class OrderType(str, Enum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"               # timeInForce=GTX(post-only) 만 쓴다
    STOP_MARKET = "STOP_MARKET"


class Status(str, Enum):
    NEW = "NEW"
    PARTIALLY_FILLED = "PARTIALLY_FILLED"
    FILLED = "FILLED"
    CANCELED = "CANCELED"
    EXPIRED = "EXPIRED"          # GTX 가 즉시 체결될 가격이라 거절, reduceOnly 무효 등
    REJECTED = "REJECTED"

    @property
    def is_open(self) -> bool:
        return self in (Status.NEW, Status.PARTIALLY_FILLED)


class Liquidity(str, Enum):
    MAKER = "MAKER"
    TAKER = "TAKER"


@dataclass
class Fill:
    client_id: str
    ts: datetime
    side: Side
    qty: float
    price: float
    fee: float                   # USDT, 양수 = 지불
    liquidity: Liquidity


@dataclass
class Order:
    client_id: str
    symbol: str
    side: Side
    type: OrderType
    qty: float
    price: float | None = None           # LIMIT
    stop_price: float | None = None      # STOP_MARKET
    reduce_only: bool = False
    status: Status = Status.NEW
    filled_qty: float = 0.0
    avg_price: float = 0.0
    placed_ts: datetime | None = None
    updated_ts: datetime | None = None
    reason: str = ""                     # 거절·만료 사유
    exchange_id: int | None = None       # 실주문 orderId

    @property
    def remaining(self) -> float:
        return round(self.qty - self.filled_qty, 8)

    def to_dict(self) -> dict:
        d = asdict(self)
        for k in ("side", "type", "status"):
            d[k] = d[k].value
        for k in ("placed_ts", "updated_ts"):
            d[k] = d[k].isoformat() if d[k] else None
        return d

    @classmethod
    def from_dict(cls, d: dict) -> Order:
        d = dict(d)
        d["side"], d["type"], d["status"] = Side(d["side"]), OrderType(d["type"]), Status(d["status"])
        for k in ("placed_ts", "updated_ts"):
            d[k] = datetime.fromisoformat(d[k]) if d[k] else None
        return cls(**d)


@dataclass
class Position:
    symbol: str
    qty: float = 0.0                     # +롱 / -숏
    entry_price: float = 0.0
    realized_pnl: float = 0.0            # 수수료 제외
    fees: float = 0.0

    def apply(self, side: Side, qty: float, price: float, fee: float) -> float:
        """체결 반영. 이번 체결로 실현된 손익(수수료 제외)을 돌려준다."""
        signed = side.sign * qty
        realized = 0.0
        if self.qty == 0 or (self.qty > 0) == (signed > 0):          # 신규·추가
            new = self.qty + signed
            self.entry_price = (self.entry_price * abs(self.qty) + price * qty) / abs(new)
            self.qty = new
        else:                                                         # 감소·반전
            closing = min(qty, abs(self.qty))
            realized = closing * (price - self.entry_price) * (1 if self.qty > 0 else -1)
            self.qty = round(self.qty + signed, 8)
            if self.qty == 0:
                self.entry_price = 0.0
            elif (self.qty > 0) == (signed > 0):                      # 반전
                self.entry_price = price
        self.realized_pnl += realized
        self.fees += fee
        return realized


@dataclass
class BookTop:
    ts: datetime                         # 거래소 시각
    bids: list[tuple[float, float]]      # [(가격, 수량)] 최우선부터
    asks: list[tuple[float, float]]

    @property
    def best_bid(self) -> float:
        return self.bids[0][0]

    @property
    def best_ask(self) -> float:
        return self.asks[0][0]

    @property
    def mid(self) -> float:
        return (self.best_bid + self.best_ask) / 2


@dataclass
class TradePrint:
    ts: datetime
    agg_id: int
    price: float
    qty: float
    buyer_maker: bool                    # True = 매도 주도 체결 (매수 호가가 맞음)


@dataclass
class FeeSchedule:
    maker_bps: float
    taker_bps: float

    def fee(self, notional: float, liquidity: Liquidity) -> float:
        bps = self.maker_bps if liquidity is Liquidity.MAKER else self.taker_bps
        return notional * bps / 1e4


@dataclass
class OrderEvent:
    """브로커가 알리는 상태 변화 (체결 로그 WOO-99 가 구독)."""
    ts: datetime
    client_id: str
    kind: str                            # placed | filled | canceled | expired | rejected | triggered
    detail: dict = field(default_factory=dict)
