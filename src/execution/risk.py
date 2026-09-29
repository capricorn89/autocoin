"""리스크 가드: 브로커를 감싸 모든 주문이 한도를 통과해야 나가게 한다 (WOO-26).

페이퍼·실주문 공용. 한도 값은 스펙(src/overnight/spec_v1.yaml 의 risk 블록)에서 온다.

상태
 - active : 정상
 - paused : 연속 손실 한도 도달. 신규 진입 금지, 청산(reduceOnly)·취소만 허용. 사람이 resume()
 - halted : 누적 손실 한도·실행 이상·킬 스위치. 신규 진입 금지. 사람이 확인 후 reset_halt()
어느 상태든 포지션을 줄이는 주문과 취소는 막지 않는다 (막으면 청산을 못 한다).

상태는 파일에 저장해 재시작해도 유지된다. 킬 스위치 파일이 있으면 즉시 halted.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .base import Broker, OrderRejected
from .market import StaleMarketData
from .types import Order, OrderEvent, OrderType, Side, Status


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class RiskLimits:
    max_abs_position: float
    test_stop_cumulative_usd: float          # 음수. 누적 손익이 이 값 이하면 halted
    pause_after_consecutive_losses: int
    max_clock_offset_ms: float
    max_data_stale_s: float
    max_consecutive_rejects: int

    @classmethod
    def from_spec(cls, risk: dict) -> RiskLimits:
        return cls(**{k: risk[k] for k in cls.__dataclass_fields__})


@dataclass
class RiskState:
    status: str = "active"                   # active | paused | halted
    reason: str = ""
    since: str | None = None
    cumulative_pnl: float = 0.0              # 밤 단위 확정 손익 (수수료·펀딩 포함)
    consecutive_losses: int = 0
    reject_streak: int = 0
    nights: list[dict] = field(default_factory=list)


class RiskHalt(OrderRejected):
    """리스크 상태 때문에 주문 거부."""


class RiskGuard:
    """Broker 를 감싸는 가드. Broker 인터페이스를 그대로 제공한다."""

    def __init__(self, broker: Broker, limits: RiskLimits, state_file: str | Path,
                 kill_file: str | Path, alert: Callable[[str], None] = print,
                 clock: Callable[[], datetime] = _utc_now):
        self.broker = broker
        self.limits = limits
        self.symbol = broker.symbol
        self.mode = broker.mode
        self._state_file = Path(state_file)
        self._kill_file = Path(kill_file)
        self._alert = alert
        self._clock = clock
        self.state = RiskState(**json.loads(self._state_file.read_text())) if self._state_file.exists() else RiskState()

    # ------------------------------------------------------------ 상태 전이
    def _set(self, status: str, reason: str) -> None:
        if self.state.status == "halted" and status != "halted":
            return                                           # halted 는 reset_halt 로만 푼다
        if (self.state.status, self.state.reason) == (status, reason):
            return
        self.state.status, self.state.reason, self.state.since = status, reason, self._clock().isoformat()
        self._save()
        self._alert(f"[리스크 {status}] {self.symbol}: {reason}")

    def halt(self, reason: str) -> None:
        self._set("halted", reason)

    def reset_halt(self, note: str) -> None:
        """사람이 원인을 확인한 뒤 호출. 킬 스위치 파일이 남아 있으면 풀리지 않는다."""
        if self._kill_file.exists():
            raise RiskHalt(f"킬 스위치 파일이 있습니다: {self._kill_file}")
        self.state.status, self.state.reason, self.state.since = "active", f"수동 해제: {note}", self._clock().isoformat()
        self.state.reject_streak = 0
        self._save()
        self._alert(f"[리스크 active] {self.symbol}: 수동 해제 — {note}")

    def resume(self, note: str) -> None:
        if self.state.status == "paused":
            self.state.status, self.state.reason = "active", f"재개: {note}"
            self.state.consecutive_losses = 0
            self._save()
            self._alert(f"[리스크 active] {self.symbol}: 재개 — {note}")

    def can_enter(self) -> bool:
        self._check_kill()
        return self.state.status == "active"

    # ------------------------------------------------------------ 점검
    def check_health(self, clock_offset_ms: float | None = None, data_age_s: float | None = None) -> None:
        """스케줄러가 주문 전·주기적으로 호출. 한도 초과면 halted."""
        self._check_kill()
        if clock_offset_ms is not None and abs(clock_offset_ms) > self.limits.max_clock_offset_ms:
            self.halt(f"시계 오프셋 {clock_offset_ms:.0f}ms > {self.limits.max_clock_offset_ms:.0f}ms")
        if data_age_s is not None and data_age_s > self.limits.max_data_stale_s:
            self.halt(f"시장 데이터 {data_age_s:.0f}s stale > {self.limits.max_data_stale_s:.0f}s")

    def check_position(self, expected_qty: float) -> None:
        actual = self.broker.position().qty
        if abs(actual - expected_qty) > 1e-9:
            self.halt(f"포지션 불일치: 기대 {expected_qty}, 실제 {actual}")

    def record_night(self, entry_date: str, pnl_usd: float, detail: dict | None = None) -> None:
        """밤 하나가 끝나면(청산 확인 후) 확정 손익을 넣는다. 한도 판정은 여기서."""
        s = self.state
        s.cumulative_pnl += pnl_usd
        s.consecutive_losses = s.consecutive_losses + 1 if pnl_usd < 0 else 0
        s.nights.append({"entry_date": entry_date, "pnl": pnl_usd, **(detail or {})})
        self._save()
        if s.cumulative_pnl <= self.limits.test_stop_cumulative_usd:
            self.halt(f"누적 손익 {s.cumulative_pnl:+.2f} ≤ {self.limits.test_stop_cumulative_usd:+.2f} USD — 테스트 중단")
        elif s.consecutive_losses >= self.limits.pause_after_consecutive_losses:
            self._set("paused", f"연속 손실 {s.consecutive_losses}밤")

    def _check_kill(self) -> None:
        if self._kill_file.exists():
            self.halt(f"킬 스위치 파일 {self._kill_file}")

    # ------------------------------------------------------------ 주문 전 검사
    def _reduces(self, side: Side, qty: float) -> bool:
        pos = self.broker.position().qty
        return pos != 0 and (pos > 0) != (side is Side.BUY) and qty <= abs(pos) + 1e-9

    def _pre_trade(self, side: Side, qty: float, reduce_only: bool) -> None:
        self._check_kill()
        if reduce_only or self._reduces(side, qty):
            return                                           # 청산 방향은 항상 허용
        if self.state.status != "active":
            raise RiskHalt(f"{self.state.status}: {self.state.reason} — 신규 진입 금지")
        pos = self.broker.position().qty
        pending = sum(o.remaining * o.side.sign for o in self.broker.open_orders()
                      if not o.reduce_only and o.type is not OrderType.STOP_MARKET)
        worst = pos + pending + side.sign * qty
        if abs(worst) > self.limits.max_abs_position + 1e-9:
            self._alert(f"[리스크] 포지션 상한 초과 주문 거부: 현재 {pos}, 대기 {pending}, 주문 {side.value} {qty}")
            raise RiskHalt(f"포지션 상한 {self.limits.max_abs_position} 초과 (결과 {worst})")

    def _after(self, o: Order) -> Order:
        if o.status is Status.REJECTED:
            self._rejected(o.reason)
        else:
            self.state.reject_streak = 0
            self._save()
        return o

    def _rejected(self, reason: str) -> None:
        self.state.reject_streak += 1
        self._save()
        if self.state.reject_streak >= self.limits.max_consecutive_rejects:
            self.halt(f"주문 거부 {self.state.reject_streak}연속: {reason}")

    def _guarded(self, fn, *args, **kwargs) -> Order:
        try:
            return self._after(fn(*args, **kwargs))
        except RiskHalt:
            raise
        except StaleMarketData as e:
            self.halt(f"시장 데이터 stale: {e}")
            raise
        except OrderRejected as e:
            self._rejected(str(e))
            raise

    # ------------------------------------------------------------ Broker 인터페이스
    def place_market(self, side: Side, qty: float, client_id: str, reduce_only: bool = False) -> Order:
        self._pre_trade(side, qty, reduce_only)
        return self._guarded(self.broker.place_market, side, qty, client_id, reduce_only=reduce_only)

    def place_limit_gtx(self, side: Side, qty: float, price: float, client_id: str,
                        reduce_only: bool = False) -> Order:
        self._pre_trade(side, qty, reduce_only)
        return self._guarded(self.broker.place_limit_gtx, side, qty, price, client_id, reduce_only=reduce_only)

    def place_stop_market(self, side: Side, qty: float, stop_price: float, client_id: str) -> Order:
        self._check_kill()                                   # 손절은 reduceOnly — 상태와 무관하게 허용
        if not self._reduces(side, qty):
            raise RiskHalt("손절 주문은 현재 포지션을 줄이는 방향·수량이어야 합니다")
        return self._guarded(self.broker.place_stop_market, side, qty, stop_price, client_id)

    def cancel(self, client_id: str) -> Order:
        return self.broker.cancel(client_id)

    def cancel_all(self) -> list[Order]:
        return self.broker.cancel_all()

    def get_order(self, client_id: str) -> Order:
        return self.broker.get_order(client_id)

    def open_orders(self) -> list[Order]:
        return self.broker.open_orders()

    def position(self):
        return self.broker.position()

    def fills(self):
        return self.broker.fills()

    def poll(self) -> list[OrderEvent]:
        return self.broker.poll()

    def subscribe(self, fn: Callable[[OrderEvent], None]) -> None:
        self.broker.subscribe(fn)

    def fills_since(self, since: datetime):
        return self.broker.fills_since(since)

    # ------------------------------------------------------------ 저장
    def _save(self) -> None:
        self._state_file.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=1))
        tmp.replace(self._state_file)
