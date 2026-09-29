"""오버나잇 스케줄러: 스펙 v1 대로 한국 장 기준 밤 보유를 실행한다 (WOO-98).

    python -m src.overnight.runner                          # paper (기본)
    python -m src.overnight.runner --mode live --confirm-live
    python -m src.overnight.runner --schedule               # 다음 밤 일정만 출력
    python -m src.overnight.runner --status                 # 상태 파일·리스크 상태 출력

밤 하나: 진입(A 시장가 / B GTX 재호가 → 시장가) → 체결가 기준 서버측 손절 → 보유(손절 발동 감시)
       → 청산(A 시장가 / B GTX → 시장가, flat 마감) → 손익 확정·리스크 기록 → 개장 후 잔존 점검.
단계마다 상태 파일에 저장하고, 재시작하면 거래소의 실제 포지션·미체결과 대조해 이어간다.
페이퍼·라이브는 브로커만 다르고 같은 코드 경로를 탄다.
"""
from __future__ import annotations

import argparse
import json
import logging
import time as _time
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from ..execution.base import OrderRejected
from ..execution.market import MarketView, StaleMarketData
from ..execution.risk import RiskGuard
from ..execution.types import Fill, OrderType, Side, Status
from .spec import SPEC_PATH, Night, Spec, SpecError

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[2]
STATE_ROOT = REPO / "data" / "overnight_live"
# 모드별 기본 스펙: 라이브는 0.1 단위(v2), 페이퍼는 1.00(v1). 2026-09-29 사용자 결정
DEFAULT_SPEC = {"paper": SPEC_PATH, "live": SPEC_PATH.with_name("spec_v2.yaml")}


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------- 달력 교차검증
def calendar_diffs(spec: Spec) -> list[str]:
    """스펙 수동 달력 vs exchange_calendars XKRX (커버 구간). 다른 날짜 목록 (ISO)."""
    import exchange_calendars as xc
    a, b = spec.covers
    xk = {d.date() for d in xc.get_calendar("XKRX").sessions_in_range(a.isoformat(), b.isoformat())}
    mine = set(spec.sessions())
    return sorted(d.isoformat() for d in xk ^ mine)


# ---------------------------------------------------------------- 상태
@dataclass
class NightState:
    entry_date: str
    exit_date: str
    method: str
    phase: str = "pending"          # pending | entering | holding | exiting | done | skipped
    qty: float = 0.0
    entry_price: float = 0.0
    exit_price: float = 0.0
    stop_id: str | None = None
    stop_price: float | None = None
    stop_seq: int = 0                # 손절을 다시 걸 때마다 새 client_id (같은 id 는 멱등 처리돼 재주문이 안 된다)
    fees: float = 0.0
    funding: float = 0.0
    pnl: float | None = None
    started_at: str | None = None
    ended_at: str | None = None
    notes: list[str] = field(default_factory=list)


class Runner:
    def __init__(self, spec: Spec, broker: RiskGuard, market: MarketView, mode: str,
                 state_dir: Path, notify: Callable[[str], None] = print,
                 health: Callable[[], dict] | None = None,
                 funding: Callable[[datetime, datetime, float], float] | None = None,
                 clock: Callable[[], datetime] = _utc_now, sleep: Callable[[float], None] = _time.sleep,
                 calendar_check: Callable[[Spec], list[str]] = calendar_diffs):
        self.spec, self.broker, self.market, self.mode = spec, broker, market, mode
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._notify, self._health, self._funding = notify, health, funding
        self._clock, self._sleep = clock, sleep
        self.qty = float(spec.raw["qty"])
        self.orders_cfg = spec.raw["orders"]
        self._events = self.state_dir / "events.jsonl"
        self._state_file = self.state_dir / "runner.json"
        self.night: NightState | None = None
        self.history: dict[str, dict] = {}
        self._load()
        self.bad_dates = set(calendar_check(spec))
        if self.bad_dates:
            self.alert(f"달력 불일치(XKRX vs 스펙): {sorted(self.bad_dates)} — 해당 날짜 밤은 실행하지 않는다")
        broker.subscribe(lambda e: self._log({"event": e.kind, "client_id": e.client_id,
                                              "ts": e.ts.isoformat(), **e.detail}))

    # ------------------------------------------------------------ 공통
    def now(self) -> datetime:
        return self._clock()

    def alert(self, text: str) -> None:
        log.warning(text)
        self._log({"event": "alert", "text": text})
        try:
            self._notify(f"[overnight {self.mode}] {text}")
        except Exception:                                   # 알림 실패로 매매를 멈추지 않는다
            log.exception("알림 실패")

    def _log(self, rec: dict) -> None:
        rec = {"logged_at": self.now().isoformat(), "mode": self.mode, **rec}
        with self._events.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def _save(self) -> None:
        tmp = self._state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"night": asdict(self.night) if self.night else None,
                                   "history": self.history}, ensure_ascii=False, indent=1))
        tmp.replace(self._state_file)

    def _load(self) -> None:
        if self._state_file.exists():
            s = json.loads(self._state_file.read_text())
            self.night = NightState(**s["night"]) if s.get("night") else None
            self.history = s.get("history", {})

    def _phase(self, phase: str, note: str = "") -> None:
        self.night.phase = phase
        if note:
            self.night.notes.append(f"{self.now():%H:%M:%S} {note}")
        self._save()
        self._log({"event": "phase", "entry_date": self.night.entry_date, "phase": phase, "note": note})

    def _cid(self, leg: str, n: int = 0) -> str:
        return f"on-{self.night.entry_date.replace('-', '')}-{leg}{n}"

    def wait_until(self, t: datetime, hold_check: bool = False) -> None:
        """t 까지 대기. 보유 중이면 60초마다 손절 발동·킬 스위치 확인."""
        while (left := (t - self.now()).total_seconds()) > 0:
            self._sleep(min(left, 60.0 if hold_check else 30.0))
            if hold_check and self.night and self.night.phase == "holding":
                self.broker.poll()
                if self.broker.position().qty == 0:
                    self._stopped_out()
                    return

    # ------------------------------------------------------------ 건강 검사
    def healthy(self) -> bool:
        info = self._health() if self._health else {}
        try:
            info.setdefault("data_age_s", (self.now() - self.market.book().ts).total_seconds())
        except StaleMarketData as e:
            self.broker.halt(f"시장 데이터 없음: {e}")
            return False
        self.broker.check_health(clock_offset_ms=info.get("clock_offset_ms"), data_age_s=info["data_age_s"])
        return self.broker.state.status != "halted"

    # ------------------------------------------------------------ 주문 실행 (A/B)
    def _work(self, side: Side, qty: float, method: str, limit_from: datetime | None, market_at: datetime,
              reduce_only: bool, leg: str) -> list[Fill]:
        """A: market_at 에 시장가. B: limit_from~market_at 동안 최우선 호가 GTX → 남은 수량 시장가.

        B 는 reprice_every 초마다 최우선 호가를 다시 보고, 우리 가격이 아직 최우선이면 그대로 둔다
        (대기열 순번 보존). 밀렸으면 취소 후 새 최우선에 다시 건다.
        """
        ids: list[str] = []

        def filled() -> float:
            return sum(f.qty for f in self.broker.fills() if f.client_id in ids)

        def top() -> float | None:
            try:
                b = self.market.book()
            except StaleMarketData:
                return None
            return b.best_bid if side is Side.BUY else b.best_ask

        if method == "B" and limit_from is not None:
            self.wait_until(limit_from)
            reprice = float(self.orders_cfg["B"]["reprice_every"])
            o = None
            while self.now() < market_at and qty - filled() > 1e-9:
                if o is None or not o.status.is_open:
                    px = top()
                    if px is None:
                        break                                # 호가를 모르면 바로 시장가로
                    cid = self._cid(leg, len(ids))
                    ids.append(cid)
                    o = self.broker.place_limit_gtx(side, round(qty - filled(), 8), px, cid, reduce_only=reduce_only)
                    if o.status is Status.EXPIRED:           # 그 사이 호가가 움직여 즉시 체결될 가격이 됨
                        self._sleep(1.0)
                        continue
                t_end = min(self.now() + timedelta(seconds=reprice), market_at)
                while o.status.is_open and self.now() < t_end:
                    self._sleep(1.0)
                    self.broker.poll()
                if o.status.is_open and self.now() < market_at and top() != o.price:
                    self._cancel_quietly(o.client_id)
            if o is not None and o.status.is_open:
                self._cancel_quietly(o.client_id)
        self.wait_until(market_at)
        rest = round(qty - filled(), 8)
        min_notional = float(self.orders_cfg.get("min_notional_usd", 0.0))
        if rest > 1e-9 and not reduce_only and filled() > 0 and min_notional:
            px = top() or 0.0
            if rest * px < min_notional:                     # 거래소 MIN_NOTIONAL 에 걸려 거부될 잔량
                self.night.notes.append(f"진입 잔량 {rest} (${rest * px:.2f}) < 최소 ${min_notional:g} — 체결분만 보유")
                rest = 0.0
        if rest > 1e-9:
            cid = self._cid(leg, 99)
            ids.append(cid)
            self.broker.place_market(side, rest, cid, reduce_only=reduce_only)
            self.broker.poll()
        return [f for f in self.broker.fills() if f.client_id in ids]

    def _cancel_quietly(self, cid: str) -> None:
        try:
            self.broker.cancel(cid)
        except OrderRejected:
            pass                                             # 그 사이 체결·만료됨
        self.broker.poll()

    # ------------------------------------------------------------ 밤 단계
    def enter(self, n: Night) -> None:
        self.night = NightState(n.entry_date.isoformat(), n.exit_date.isoformat(), n.method,
                                started_at=self.now().isoformat())
        t = self.spec.order_times(n)
        self.wait_until(t.get("entry_limit", t["entry_market"]))
        if not self.healthy() or not self.broker.can_enter():
            return self._skip(f"진입 불가: 리스크 {self.broker.state.status} {self.broker.state.reason}")
        if self.broker.position().qty != 0:
            self.broker.halt(f"진입 전 포지션 {self.broker.position().qty} — 기대 0")
            return self._skip("예상 밖 포지션")
        if self.mode == "live" and hasattr(self.broker.broker, "verify_account"):
            bad = self.broker.broker.verify_account(self.spec.raw["account"])
            if bad:
                self.broker.halt(f"계정 설정 불일치: {bad}")
                return self._skip("계정 설정 불일치")
        self._phase("entering", f"방식 {n.method}")
        try:
            fills = self._work(Side.BUY, self.qty, n.method, t.get("entry_limit"), t["entry_market"],
                               reduce_only=False, leg="e")
        except OrderRejected as e:
            prefix = self._cid("e", 0)[:-1]
            fills = [f for f in self.broker.fills() if f.client_id.startswith(prefix)]
            self.alert(f"진입 주문 거부: {e}")
        qty = sum(f.qty for f in fills)
        if qty <= 1e-9:
            return self._skip("진입 체결 없음")
        self.night.qty = qty
        self.night.entry_price = sum(f.qty * f.price for f in fills) / qty
        self.night.fees += sum(f.fee for f in fills)
        self._place_stop()
        self._phase("holding", f"진입 {qty} @ {self.night.entry_price:.2f} (maker {sum(f.qty for f in fills if f.liquidity.value == 'MAKER'):g})")
        self.alert(f"{self.night.entry_date} 진입 {qty} @ {self.night.entry_price:.2f} [{n.method}], 손절 {self.night.stop_price}")

    def _place_stop(self) -> None:
        pct = float(self.spec.raw["stop"]["pct_from_entry"])
        price = round(self.night.entry_price * (1 + pct / 100), 2)
        self.night.stop_id, self.night.stop_price = self._cid("s", self.night.stop_seq), price
        self.night.stop_seq += 1
        try:
            self.broker.place_stop_market(Side.SELL, self.night.qty, price, self.night.stop_id)
        except OrderRejected as e:
            self.alert(f"비상 손절 주문 실패 — 보호 없이 보유 중: {e}")
        self._save()

    def exit(self, n: Night) -> None:
        t = self.spec.order_times(n)
        if self.night.phase == "holding":
            self.wait_until(t.get("exit_limit", t["exit_market"]), hold_check=True)
        if self.night.phase in ("done", "skipped"):
            return
        self._phase("exiting")
        pos = self.broker.position().qty
        fills: list[Fill] = []
        if pos > 0:
            try:
                fills = self._work(Side.SELL, pos, n.method, t.get("exit_limit"), t["exit_market"],
                                   reduce_only=True, leg="x")
            except OrderRejected as e:
                self.alert(f"청산 주문 거부: {e} — flat 마감에 강제 청산 시도")
        if (left := self.broker.position().qty) != 0:
            self.alert(f"flat 마감 초과: 포지션 {left} → 강제 시장가")
            self.broker.place_market(Side.SELL if left > 0 else Side.BUY, abs(left), self._cid("f"), reduce_only=True)
            self.broker.poll()
        self._finalize("청산")

    def _stopped_out(self) -> None:
        self.alert(f"{self.night.entry_date} 비상 손절 발동 (손절가 {self.night.stop_price})")
        self._finalize("손절")

    def _finalize(self, how: str) -> None:
        if self.broker.open_orders():
            self.broker.cancel_all()
        start = datetime.fromisoformat(self.night.started_at)
        mine = [f for f in self.broker.fills_since(start) if f.client_id.startswith(f"on-{self.night.entry_date.replace('-', '')}")
                or f.client_id.startswith("order:")]
        buys = [f for f in mine if f.side is Side.BUY]
        sells = [f for f in mine if f.side is Side.SELL]
        sq = sum(f.qty for f in sells)
        self.night.exit_price = sum(f.qty * f.price for f in sells) / sq if sq else 0.0
        self.night.fees = sum(f.fee for f in mine)
        end = self.now()
        self.night.funding = self._funding(start, end, self.night.qty) if self._funding else 0.0
        gross = sum(f.qty * f.price for f in sells) - sum(f.qty * f.price for f in buys)
        self.night.pnl = gross - self.night.fees + self.night.funding
        self.night.ended_at = end.isoformat()
        self._phase("done", f"{how} {sq} @ {self.night.exit_price:.2f}, 손익 {self.night.pnl:+.4f}")
        self.broker.record_night(self.night.entry_date, self.night.pnl,
                                 {"method": self.night.method, "how": how, "entry": self.night.entry_price,
                                  "exit": self.night.exit_price, "fees": self.night.fees, "funding": self.night.funding})
        self._log({"event": "night", **asdict(self.night)})
        self.history[self.night.entry_date] = asdict(self.night)
        self._save()
        self.alert(f"{self.night.entry_date} {how}: {self.night.entry_price:.2f} → {self.night.exit_price:.2f}, "
                   f"손익 {self.night.pnl:+.4f} USDT (수수료 {self.night.fees:.4f}, 펀딩 {self.night.funding:+.4f}) "
                   f"| 누적 {self.broker.state.cumulative_pnl:+.2f}")

    def _skip(self, reason: str) -> None:
        self.night.phase = "skipped"
        self.night.notes.append(reason)
        self.history[self.night.entry_date] = asdict(self.night)
        self._save()
        self._log({"event": "night", **asdict(self.night)})
        self.alert(f"{self.night.entry_date} 밤 스킵: {reason}")

    def residual_check(self, n: Night) -> None:
        t = self.spec.order_times(n) if n.method else None
        if t:
            self.wait_until(t["residual_check"])
        if (pos := self.broker.position().qty) != 0:
            self.broker.halt(f"개장 후 잔존 포지션 {pos}")
            self.broker.place_market(Side.SELL if pos > 0 else Side.BUY, abs(pos), self._cid("r"), reduce_only=True)
            self.alert(f"잔존 포지션 {pos} 강제 청산 — 사고 기록 필요")

    # ------------------------------------------------------------ 복구·루프
    def recover(self) -> None:
        """시작 시 상태 파일과 거래소 실제 상태 대조."""
        pos = self.broker.position().qty
        open_ = self.broker.open_orders()
        active = self.night and self.night.phase in ("entering", "holding", "exiting")
        if active and pos > 0:
            self.night.qty = pos
            if not any(o.type is OrderType.STOP_MARKET and o.status.is_open for o in open_):
                self.alert("복구: 손절 주문이 없어 다시 건다")
                self._place_stop()
            self._phase("holding", "재시작 복구")
        elif active and pos == 0:
            if self.night.phase == "entering":
                self._skip("재시작: 진입 도중 중단, 포지션 없음")
            else:
                self._finalize("재시작 복구(이미 flat)")
        elif pos != 0:
            self.broker.halt(f"기록에 없는 포지션 {pos}")
            self.alert(f"기록에 없는 포지션 {pos} — 사람 확인 필요 (자동 청산하지 않음)")
        elif open_:
            self.alert(f"기록에 없는 미체결 {len(open_)}건 취소")
            self.broker.cancel_all()

    def next_night(self) -> Night | None:
        now = self.now()
        for n in self.spec.nights():
            if n.entry_date.isoformat() in self.history:
                continue
            if n.exit_open <= now:
                continue
            return n
        return None

    def run_night(self, n: Night) -> None:
        if n.skip or {n.entry_date.isoformat(), n.exit_date.isoformat()} & self.bad_dates:
            self.night = NightState(n.entry_date.isoformat(), n.exit_date.isoformat(), n.method or "-",
                                    started_at=self.now().isoformat())
            return self._skip(n.skip or "달력 불일치")
        resuming = self.night and self.night.entry_date == n.entry_date.isoformat() and self.night.phase == "holding"
        if not resuming:
            t = self.spec.order_times(n)
            if self.now() > t["entry_give_up"]:
                self.night = NightState(n.entry_date.isoformat(), n.exit_date.isoformat(), n.method,
                                        started_at=self.now().isoformat())
                return self._skip("진입 시각 지남 (늦게 시작)")
            self.enter(n)
        if self.night.phase == "holding":
            self.exit(n)
            self.residual_check(n)

    def run_forever(self, max_nights: int | None = None) -> None:
        self.recover()
        done = 0
        while max_nights is None or done < max_nights:
            try:
                n = self.next_night()
            except SpecError as e:
                self.alert(f"스펙 달력 범위 끝: {e}")
                return
            if n is None:
                self.alert("남은 밤 없음 (스펙 달력 범위 끝) — 종료")
                return
            self.run_night(n)
            done += 1


# ---------------------------------------------------------------- 조립
def public_funding(symbol: str) -> Callable[[datetime, datetime, float], float]:
    """공개 펀딩 이력으로 롱 보유 펀딩 계산 (페이퍼용). 롱은 +rate 면 지불."""
    from ..binance_data import fetch_funding_rates

    def f(start: datetime, end: datetime, qty: float) -> float:
        df = fetch_funding_rates(symbol, int(start.timestamp() * 1000), int(end.timestamp() * 1000))
        df = df[(df.index > start) & (df.index < end)]
        return float(-(df["fundingRate"] * df["markPrice"] * qty).sum())
    return f


def build(mode: str, confirm_live: bool, spec_path: Path | None = None) -> Runner:
    from ..exchange.rest import FuturesRestClient, measure_clock_offset
    from ..execution.base import make_broker
    from ..execution.market import DbMarketView
    from ..execution.risk import RiskLimits
    from ..execution.types import FeeSchedule
    from ..notify import TelegramNotifier
    spec = Spec.load(spec_path or DEFAULT_SPEC[mode])
    sym = spec.raw["symbol"]
    fees = FeeSchedule(spec.raw["account"]["fees_bps"]["maker"], spec.raw["account"]["fees_bps"]["taker"])
    state_dir = STATE_ROOT / mode
    market = DbMarketView(sym, max_stale_s=spec.raw["risk"]["max_data_stale_s"])
    if mode == "paper":
        inner = make_broker("paper", symbol=sym, market=market, fees=fees, state_file=state_dir / "paper_exchange.json")
    else:
        inner = make_broker("live", confirm_live=confirm_live, symbol=sym, fees=fees)
    tg = TelegramNotifier.from_env()
    notify = tg.send if tg else print
    guard = RiskGuard(inner, RiskLimits.from_spec(spec.raw["risk"]), state_dir / "risk.json",
                      state_dir / "KILL", alert=notify)
    public = FuturesRestClient()

    def health() -> dict:
        return {"clock_offset_ms": measure_clock_offset(public, 3)["offset_ms"]}

    funding = (lambda s, e, q: inner.funding_between(s, e)) if mode == "live" else public_funding(sym)
    r = Runner(spec, guard, market, mode, state_dir, notify=notify, health=health, funding=funding)
    r._log({"event": "start", "spec_sha256": spec.sha256, "spec_version": spec.raw["version"]})
    return r


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.overnight.runner")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--confirm-live", action="store_true")
    ap.add_argument("--schedule", action="store_true", help="다음 밤 일정만 출력")
    ap.add_argument("--status", action="store_true", help="상태 출력")
    ap.add_argument("--spec", type=Path, help="스펙 파일 (기본: paper=v1, live=v2)")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if args.schedule:
        spec = Spec.load(args.spec or DEFAULT_SPEC[args.mode])
        print(f"스펙 v{spec.raw['version']} ({args.mode}), 수량 {spec.raw['qty']}")
        print("달력 불일치(XKRX):", calendar_diffs(spec) or "없음")
        now = _utc_now()
        for n in [n for n in spec.nights() if n.exit_open > now][:10]:
            print(n.entry_date, "→", n.exit_date, n.method or "-", n.skip or "")
        return
    if args.status:
        d = STATE_ROOT / args.mode
        for name in ("runner.json", "risk.json"):
            p = d / name
            print(f"--- {p}\n{p.read_text() if p.exists() else '(없음)'}")
        return
    if args.mode == "live" and not args.confirm_live:
        raise SystemExit("live 모드는 --confirm-live 가 필요합니다.")
    r = build(args.mode, args.confirm_live, args.spec)
    try:
        r.run_forever()
    except Exception as e:
        # launchd 가 60초 뒤 재시작하고 recover() 로 이어가지만, 죽은 사실은 사람이 알아야 한다
        r.alert(f"스케줄러 예외 종료 ({type(e).__name__}: {str(e)[:200]}) — launchd 재시작 후 복구 예정")
        raise


if __name__ == "__main__":
    main()
