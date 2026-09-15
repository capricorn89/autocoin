"""체결 추상화: PaperBroker(시뮬) + BinanceBroker(실주문, opt-in).

포지션/현금/수수료 회계를 담당. 백테스트와 페이퍼 라이브가 PaperBroker 를 공유한다.
실주문(BinanceBroker)은 execution.mode='live' + API 키가 있을 때만 동작한다.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime


@dataclass
class Trade:
    ts: datetime
    qty: float       # +매수 / -매도
    price: float     # 체결가(슬리피지 포함)
    fee: float
    reason: str = ""


@dataclass
class PaperBroker:
    """시장가 시뮬 체결.

    cash 는 거래 현금흐름 누적(매수 시 감소). 포트폴리오 가치 = position*price + cash.
    """
    taker_fee_bps: float = 5.0
    slippage_bps: float = 1.0
    step_size: float = 0.01
    min_qty: float = 0.01
    min_notional: float = 5.0

    position: float = 0.0
    cash: float = 0.0
    fees_paid: float = 0.0
    trades: list[Trade] = field(default_factory=list)

    def _round_qty(self, qty: float) -> float:
        steps = round(qty / self.step_size)
        return steps * self.step_size

    def rebalance_to(self, ts: datetime, target: float, ref_price: float,
                     band: float = 0.0, reason: str = "") -> Trade | None:
        """현재 포지션을 target 으로 맞추는 시장가 주문(차이만큼)."""
        delta_qty = self._round_qty(target - self.position)
        if abs(delta_qty) < self.min_qty:
            return None
        if abs(target - self.position) <= band:
            return None
        if abs(delta_qty * ref_price) < self.min_notional:
            return None

        side = 1.0 if delta_qty > 0 else -1.0
        fill = ref_price * (1.0 + side * self.slippage_bps / 1e4)
        notional = abs(delta_qty) * fill
        fee = notional * self.taker_fee_bps / 1e4

        self.cash -= delta_qty * fill        # 매수(+qty) -> 현금 감소
        self.cash -= fee
        self.fees_paid += fee
        self.position += delta_qty

        tr = Trade(ts=ts, qty=delta_qty, price=fill, fee=fee, reason=reason)
        self.trades.append(tr)
        return tr

    def equity(self, mark_price: float) -> float:
        """시가평가 자산(복제 포트폴리오 손익)."""
        return self.position * mark_price + self.cash


class BinanceBroker:
    """실주문 브로커 (opt-in). API 키가 없으면 인스턴스화 시 막는다.

    NOTE: 실제 자금이 집행된다. execution.mode='live' 이고 키가 설정된 경우에만 사용.
    """

    def __init__(self, symbol: str, api_key: str | None, api_secret: str | None,
                 step_size: float = 0.01, min_qty: float = 0.01, min_notional: float = 5.0):
        if not api_key or not api_secret:
            raise RuntimeError(
                "BinanceBroker: API 키가 없습니다. 실주문은 BINANCE_API_KEY/SECRET 설정 + "
                "execution.mode='live' 일 때만 가능합니다.")
        self.symbol = symbol
        self.api_key = api_key
        self.api_secret = api_secret
        self.step_size = step_size
        self.min_qty = min_qty
        self.min_notional = min_notional

    def _round_qty(self, qty: float) -> float:
        return round(qty / self.step_size) * self.step_size

    def rebalance_to(self, ts, target, ref_price, band=0.0, reason=""):
        # 실주문 전송은 사용자가 명시적으로 구현/활성화해야 함.
        # 안전을 위해 기본은 NotImplemented 로 막아둔다.
        raise NotImplementedError(
            "실주문 전송은 의도적으로 비활성화되어 있습니다. 검토 후 직접 구현하세요 "
            "(서명된 POST /fapi/v1/order, MARKET, quantity=반올림된 차이).")
