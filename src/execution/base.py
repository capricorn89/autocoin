"""브로커 인터페이스. 전략·스케줄러는 이 메서드만 쓰고 페이퍼/실주문을 모른다.

모드 선택은 make_broker() 한 곳에서만 한다. 기본은 paper 이고, live 는
mode='live' 와 confirm_live=True 가 둘 다 있어야 만든다 (PLAN.md 실주문 안전장치).
"""
from __future__ import annotations

from typing import Callable, Protocol

from .types import Fill, Order, OrderEvent, Position, Side


class OrderRejected(RuntimeError):
    """주문이 거래소(또는 시뮬)·리스크 가드에서 거부됨."""


class Broker(Protocol):
    symbol: str
    mode: str                                   # "paper" | "live"

    def place_market(self, side: Side, qty: float, client_id: str, reduce_only: bool = False) -> Order: ...
    def place_limit_gtx(self, side: Side, qty: float, price: float, client_id: str,
                        reduce_only: bool = False) -> Order: ...
    def place_stop_market(self, side: Side, qty: float, stop_price: float, client_id: str) -> Order: ...
    def cancel(self, client_id: str) -> Order: ...
    def cancel_all(self) -> list[Order]: ...
    def get_order(self, client_id: str) -> Order: ...
    def open_orders(self) -> list[Order]: ...
    def position(self) -> Position: ...
    def fills(self) -> list[Fill]: ...
    def poll(self) -> list[OrderEvent]:
        """페이퍼: 새 체결로 대기 주문·손절을 진행. 실주문: 거래소 상태를 다시 읽는다."""
        ...
    def subscribe(self, fn: Callable[[OrderEvent], None]) -> None: ...


def make_broker(mode: str, *, confirm_live: bool = False, **kwargs) -> Broker:
    if mode == "paper":
        from .paper import PaperExchange
        return PaperExchange(**kwargs)
    if mode == "live":
        if not confirm_live:
            raise OrderRejected("live 모드는 --confirm-live 가 필요합니다. 기본은 paper 입니다.")
        from .live import BinanceFuturesBroker
        return BinanceFuturesBroker(**kwargs)
    raise ValueError(f"알 수 없는 모드: {mode!r} (paper | live)")
