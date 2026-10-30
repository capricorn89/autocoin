"""신호 → 주문. 장부·위험 한도·기록·알림. 페이퍼·라이브 공용 (브로커만 다르다).

흐름
    엔진 로직1 진입 판단(Signal) → 한도·데이터 게이트 → 시장가 매도 → 비상 손절(STOP_MARKET +1.5%) → 보유
    (v3) 마감 N초 전 → bid1 에 reduceOnly GTX 매수 지정가 (매초 체결 확인, 다 체결되면 그걸로 청산)
    엔진 로직1 청산 판단(Exit) → (메이커 주문 취소) → 남은 수량 reduceOnly 시장가 매수 → 비상 손절 취소 → 손익 확정 → 정지선 판정
게이트에 걸려 건너뛴 신호도 엔진은 계속 돌아 가상 결과가 signals 에 남는다 (매일 대조가 거래 단위로 맞는다).

주문은 종목마다 작업 스레드 하나(submit)에서 차례로 실행한다 — 라이브 REST 호출이 WS 처리·다른 종목 판단을 막지 않게.
장부(state.json)는 잠금 안에서만 바꾼다. 재시작하면 recover() 가 거래소(페이퍼는 시뮬 상태 파일)와 대조한다.
"""
from __future__ import annotations

import json
import logging
import math
import threading
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from ...execution.base import OrderRejected
from ...execution.types import Side, Status
from .feed import DAY, Exit, Signal
from .notify import SHORT, entry_text, exit_text, money
from .spec import Spec, SpecError, SymbolFilters, sized
from .store import Store

log = logging.getLogger(__name__)


class StartRefused(RuntimeError):
    """장부와 거래소가 맞지 않거나 다른 프로그램의 포지션·주문이 있어 시작하면 안 된다."""


def utc_day(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).date().isoformat()


def _dt(epoch: float) -> datetime:
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


@dataclass
class TraderState:
    mode: str
    spec_version: str
    started_at: float                      # 첫 시작 (페이퍼 7일 기준)
    trial_end: float
    halted: bool = False
    halt_reason: str = ""
    halt_since: Optional[float] = None
    cum_net: float = 0.0                   # 실현 손익 누적 (수수료 포함)
    cum_gross: float = 0.0
    cum_fees: float = 0.0
    n_trades: int = 0
    entries: dict = field(default_factory=dict)       # {UTC 날짜: {종목: 진입 수}}
    realized: dict = field(default_factory=dict)      # {UTC 날짜: {"net", "n", "win", "loss"}}
    positions: dict = field(default_factory=dict)     # {종목: 포지션 dict | None}
    alerted: list = field(default_factory=list)       # 하루 한 번 알림 키
    trial_summary_sent: bool = False
    last_start: Optional[float] = None
    starts: list = field(default_factory=list)        # 시작 시각 기록 (대조에서 공백 설명용)
    live_wallet_start: Optional[float] = None         # 라이브: 첫 시작 때 지갑 잔고

    @classmethod
    def load(cls, path: Path) -> Optional[TraderState]:
        if not path.exists():
            return None
        d = json.loads(path.read_text())
        known = cls.__dataclass_fields__
        return cls(**{k: v for k, v in d.items() if k in known})


class Trader:
    def __init__(self, spec: Spec, mode: str, brokers: dict, filters: dict[str, SymbolFilters], store: Store,
                 state_dir: Path, notify: Callable[[str], None] = print,
                 gate: Callable[[str, Signal], Optional[str]] = lambda sym, sig: None,
                 quote: Callable[[str], Optional[tuple]] = lambda sym: None,
                 submit: Optional[Callable[[str, Callable[[], None]], None]] = None,
                 now: Callable[[], float] = time.time,
                 wallet: Callable[[], Optional[float]] = lambda: None):
        self.spec, self.mode = spec, mode
        self.brokers = brokers                       # {종목: RiskGuard}
        self.filters = filters
        self.store = store
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.dir / "state.json"
        self.resume_file = self.dir / "resume_request.json"
        self.notify = notify
        self.gate = gate
        self.quote = quote
        self.now = now
        self.wallet = wallet
        self._submit = submit or (lambda sym, fn: fn())
        self.lock = threading.RLock()
        self.prefix = spec.raw["live"]["client_id_prefix"]
        self.max_entries = int(spec.raw["risk"]["max_entries_per_symbol_day"])
        self.stop_line = float(spec.raw["risk"]["stop_cumulative_usd"])
        self.taker_bps = float(spec.raw["fees_bps"]["taker"])
        self.p = spec.params()
        self.maker_s = spec.maker_exit_s                 # None 이면 시장가 청산만 (v1·v2)
        self.estop_pct = float(spec.raw["live"]["emergency_stop_pct"])
        self.start_balance = float(spec.raw["paper"]["start_balance_usd"])
        t0 = now()
        st = TraderState.load(self.state_file)
        if st is None:
            days = int(spec.raw["paper"]["trial_days"])
            st = TraderState(mode=mode, spec_version=spec.version, started_at=t0, trial_end=t0 + days * DAY)
        if st.spec_version != spec.version:
            raise StartRefused(f"상태 파일 스펙 {st.spec_version} ≠ 지금 스펙 {spec.version} — 새 상태 폴더로 시작하라")
        st.last_start = t0
        st.starts = (st.starts + [t0])[-100:]
        for s in spec.symbols:
            st.positions.setdefault(s, None)
        self.state = st
        self._next_poll: dict[str, float] = defaultdict(float)
        self._sig_status: dict[tuple, dict] = {}
        self.ex_positions: Optional[dict] = None           # 라이브: {종목: (positionAmt, entryPrice)} — runner 가 30초마다 갱신
        self._save()

    # ------------------------------------------------------------ 공통
    def _save(self) -> None:
        with self.lock:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(asdict(self.state), ensure_ascii=False, indent=1, default=str))
            tmp.replace(self.state_file)

    def alert(self, text: str, key: Optional[str] = None) -> None:
        """key 가 있으면 같은 key 로는 한 번만 보낸다."""
        if key is not None:
            with self.lock:
                if key in self.state.alerted:
                    return
                self.state.alerted = (self.state.alerted + [key])[-500:]
                self._save()
        log.warning(text)
        self.store.event({"event": "alert", "text": text})
        try:
            self.notify(text)
        except Exception:
            log.exception("알림 실패")

    def _cid(self, sym: str, sig_epoch: int, leg: str, n: int = 0) -> str:
        """pof-<종목3>-<YYYYMMDD>-<T>-<다리><n>  (다리: e 진입 · x 청산 · m 메이커 청산 · s 비상손절). 36자 이내."""
        d, T = divmod(int(sig_epoch), DAY)
        return f"{self.prefix}{SHORT.get(sym, sym[:3])}-{_dt(d * DAY):%Y%m%d}-{T}-{leg}{n}"

    # ------------------------------------------------------------ 요약 (알림·웹)
    def account(self) -> dict:
        with self.lock:
            st = self.state
            today = utc_day(self.now())
            r = st.realized.get(today, {})
            w = self.wallet() if self.mode == "live" else None
            balance = w if w is not None else self.start_balance + st.cum_net
            return {"balance": balance, "start_balance": self.start_balance, "today": r.get("net", 0.0), "cum": st.cum_net, "stop": self.stop_line,
                    "today_n": r.get("n", 0), "today_win": r.get("win", 0), "today_loss": r.get("loss", 0),
                    "entries": {s: st.entries.get(today, {}).get(s, 0) for s in self.spec.symbols},
                    "max_entries": self.max_entries, "halted": st.halted, "halt_reason": st.halt_reason,
                    "cum_gross": st.cum_gross, "cum_fees": st.cum_fees, "n_trades": st.n_trades}

    def positions(self) -> list[dict]:
        out = []
        with self.lock:
            for s in self.spec.symbols:
                pos = self.state.positions.get(s)
                q = self.quote(s)
                if self.ex_positions is not None:          # 라이브: 거래소 조회값 (장부와 다르면 runner 가 경고)
                    amt, entry = self.ex_positions.get(s, (0.0, 0.0))
                    out.append({"symbol": s, "qty": abs(amt), "entry": entry, "ask": q[1] if q else None}
                               if amt else {"symbol": s, "qty": 0})
                elif pos and pos.get("entry_px"):
                    out.append({"symbol": s, "qty": pos["qty"], "entry": pos["entry_px"], "ask": q[1] if q else None})
                else:
                    out.append({"symbol": s, "qty": 0})
        return out

    # ------------------------------------------------------------ 진입
    def on_entry(self, sig: Signal) -> None:
        sym, ep = sig.symbol, sig.epoch_sig
        rec = {"symbol": sym, "t_sig": ep, "day": _dt(sig.day * DAY).date().isoformat(), "p_in": sig.p_in,
               "past_bp": sig.feat.get("past_bp"), "ofi_ratio": sig.feat.get("ofi_ratio"),
               "t_in": sig.day * DAY + sig.t_in, "feat": sig.feat, "decided_at": self.now()}
        reason = self._skip_reason(sig)
        if reason is not None:
            self._signal({**rec, "status": "skipped", "skip_reason": reason})
            self.store.event({"event": "skip", "symbol": sym, "t_sig": ep, "reason": reason})
            log.info("신호 건너뜀 %s %s: %s", sym, utc_day(ep), reason)
            return
        prices = {s: sig.p_in if s == sym else (self.quote(s) or (None,))[0] for s in self.spec.symbols}
        if self.spec.sizing_rule == "equal_notional" and not all(prices.values()):
            self._signal({**rec, "status": "skipped", "skip_reason": "시세 없음 (수량 계산)"})
            return
        try:
            qty = sized(self.spec, sym, prices, self.filters)
        except SpecError as e:
            self._signal({**rec, "status": "skipped", "skip_reason": "수량 상한"})
            self.alert(f"{sym} 신호를 건너뛰었습니다 — {e}")
            return
        if self.mode == "paper":
            used = sum(p["qty"] * (p.get("entry_px") or p["assumed_in"])
                       for p in self.state.positions.values() if p)
            if used + qty * sig.p_in > self.start_balance + self.state.cum_net:   # 레버리지 1
                self._signal({**rec, "status": "skipped", "skip_reason": "증거금 부족"})
                return
        day = utc_day(ep)
        with self.lock:
            self.state.entries.setdefault(day, {})
            self.state.entries[day][sym] = self.state.entries[day].get(sym, 0) + 1
            self.state.positions[sym] = {
                "symbol": sym, "status": "opening", "source": "engine", "qty": qty, "side": -1,
                "t_sig_epoch": ep, "day": day, "deadline_epoch": sig.day * DAY + sig.deadline,
                "assumed_in": sig.p_in, "tp_px": sig.p_in * (1 - self.p.tp_bp / 1e4),
                "sl_px": sig.p_in * (1 + self.p.sl_bp / 1e4), "past_bp": sig.feat.get("past_bp"),
                "ofi_ratio": sig.feat.get("ofi_ratio"), "feat": sig.feat,
                "entry_cid": self._cid(sym, ep, "e"), "exit_n": 0}
            self._save()
        self._signal({**rec, "status": "traded"})
        if self.state.entries[day][sym] == self.max_entries:
            self.alert(f"{sym} 오늘(UTC {day}) 진입 한도 {self.max_entries}건에 도달했습니다. 남은 신호는 건너뜁니다.",
                       key=f"limit|{day}|{sym}")
        self._submit(sym, lambda: self._do_entry(sym, ep))

    def _skip_reason(self, sig: Signal) -> Optional[str]:
        sym = sig.symbol
        with self.lock:
            if self.state.halted:
                return f"정지선 ({self.state.halt_reason})"
            guard = self.brokers[sym]
            if not guard.can_enter():
                return f"리스크 {guard.state.status}: {guard.state.reason}"
            n = self.state.entries.get(utc_day(sig.epoch_sig), {}).get(sym, 0)
            if n >= self.max_entries:
                return f"하루 진입 한도 {self.max_entries}건"
            if self.state.positions.get(sym):
                return "보유 중 (이전 포지션 청산 전)"
        return self.gate(sym, sig)

    def _do_entry(self, sym: str, ep: int) -> None:
        guard = self.brokers[sym]
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos or pos["t_sig_epoch"] != ep or pos["status"] != "opening":
                return
            qty, cid = pos["qty"], pos["entry_cid"]
        try:
            actual = guard.position().qty
            if abs(actual) > 1e-12:                  # 장부는 0 인데 거래소에 포지션이 있다
                raise OrderRejected(f"장부 불일치: 장부 0, 거래소 {actual}")
            o = guard.place_market(Side.SELL, qty, cid)
        except Exception as e:
            with self.lock:
                self.state.positions[sym] = None
                self._save()
            self._signal({"symbol": sym, "t_sig": ep, "day": utc_day(ep), "status": "skipped",
                          "skip_reason": f"진입 주문 실패: {str(e)[:120]}"})
            self.alert(f"{sym} 진입 주문을 내지 못했습니다 — {type(e).__name__}: {str(e)[:200]}")
            return
        fills = [f for f in guard.fills() if f.client_id == cid]
        if o.status is not Status.FILLED and not fills:
            with self.lock:
                self.state.positions[sym] = None
                self._save()
            self.alert(f"{sym} 진입 주문이 체결되지 않았습니다 ({o.status.value} {o.reason})")
            return
        fq = sum(f.qty for f in fills) or o.filled_qty
        fpx = (sum(f.qty * f.price for f in fills) / fq) if fills else o.avg_price
        fee = sum(f.fee for f in fills) if fills else fq * fpx * self.taker_bps / 1e4
        stop_px = fpx * (1 + self.estop_pct / 1e2)
        note = None
        stop_cid = self._cid(sym, ep, "s")
        try:
            guard.place_stop_market(Side.BUY, fq, stop_px, stop_cid)
        except Exception as e:
            note = f"⚠️ 비상 손절 주문 실패: {str(e)[:120]}"
            stop_cid = None
            self.alert(f"{sym} 비상 손절(+{self.estop_pct}%) 주문을 걸지 못했습니다 — {str(e)[:200]}")
        with self.lock:
            pos = self.state.positions[sym]
            pos.update(status="open", qty=fq, entry_px=fpx, entry_fee=fee, entry_ts=self.now(),
                       stop_cid=stop_cid, stop_px=stop_px,
                       slip_in_bp=(pos["assumed_in"] - fpx) / pos["assumed_in"] * 1e4,
                       fee_estimated=not fills)
            self._save()
            e = {"symbol": sym, "qty": fq, "entry_px": fpx, "t_sig_epoch": ep, "past_bp": pos["past_bp"] or 0.0,
                 "ofi_ratio": pos["ofi_ratio"] or 0.0, "assumed_in": pos["assumed_in"], "slip_in_bp": pos["slip_in_bp"],
                 "fee": fee, "deadline_epoch": pos["deadline_epoch"], "tp_px": pos["tp_px"], "sl_px": pos["sl_px"],
                 "note": note}
        self.store.event({"event": "entry", **e, "cid": cid})
        self.notify(entry_text(e, self.positions(), self.account()))

    # ------------------------------------------------------------ 청산
    def on_exit(self, ex: Exit) -> None:
        tr, sym = ex.trade, ex.symbol
        ep = ex.day * DAY + tr.t_sig
        self.store.signal({"symbol": sym, "t_sig": ep, "day": utc_day(ep), "p_in": tr.p_in,
                           "t_out": ex.day * DAY + tr.t_out, "p_out": tr.p_out, "reason": tr.reason,
                           "engine_bp": tr.pnl_bp, "past_bp": tr.past_bp, "ofi_ratio": tr.ofi_ratio,
                           "t_in": ex.day * DAY + tr.t_in, **self._signal_status(sym, ep)})
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos or pos["t_sig_epoch"] != ep or pos.get("source") != "engine":
                return                               # 건너뛴 신호 — 가상 결과만 남긴다
            pos["exit_req"] = {"reason": tr.reason, "assumed_out": tr.p_out, "t_out": ex.day * DAY + tr.t_out}
            self._save()
        self._submit(sym, lambda: self._do_exit(sym, ep))

    def _signal_status(self, sym: str, ep: int) -> dict:
        """진입 때 남긴 상태(traded/skipped)를 청산 줄에도 그대로 잇는다."""
        if (sym, ep) not in self._sig_status:
            for r in self.store.signals():
                self._sig_status[(r["symbol"], r["t_sig"])] = {"status": r.get("status"), "skip_reason": r.get("skip_reason")}
        return self._sig_status.get((sym, ep), {"status": "skipped", "skip_reason": "진입 기록 없음 (재시작 전 신호)"})

    def _signal(self, rec: dict) -> None:
        self._sig_status[(rec["symbol"], rec["t_sig"])] = {"status": rec["status"], "skip_reason": rec.get("skip_reason")}
        self.store.signal(rec)

    def _do_exit(self, sym: str, ep: int) -> None:
        guard = self.brokers[sym]
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos or pos["t_sig_epoch"] != ep or pos["status"] != "open" or not pos.get("exit_req"):
                return
            pos["status"] = "closing"
            req = dict(pos["exit_req"])
            n = pos.get("exit_n", 0)
            cid = self._cid(sym, ep, "x", n)
            qty = pos["qty"]
            maker_cid = pos.get("maker_cid")
            self._save()
        mfills = self._close_maker(sym, maker_cid) if maker_cid else []
        mq = round(sum(f.qty for f in mfills), 8)
        if mfills and mq >= qty - 1e-9:                   # 메이커가 다 체결됐다
            self._cancel_stop(sym)
            self._finalize(sym, sum(f.qty * f.price for f in mfills) / mq, sum(f.fee for f in mfills),
                           req["reason"] + "(메이커)", req.get("assumed_out"), maker_cid)
            return
        try:
            o = guard.place_market(Side.BUY, round(qty - mq, 8), cid, reduce_only=True)
        except Exception as e:
            self._exit_failed(sym, f"{type(e).__name__}: {str(e)[:160]}")
            return
        fills = [f for f in guard.fills() if f.client_id == cid]
        if o.status is Status.EXPIRED and not fills and not mfills:   # reduceOnly 거절 = 거래소에 포지션이 없다
            if self._check_stop(sym, force=True):
                return
            self._finalize(sym, pos["entry_px"], 0.0, req["reason"] + "(포지션 없음)", req.get("assumed_out"), cid,
                           note="⚠️ 청산하려는데 포지션이 없었습니다. 거래소를 확인해 주세요.")
            guard.halt(f"{sym} 청산 시점에 포지션 없음")
            return
        if o.status is not Status.FILLED and not fills and not mfills:
            self._exit_failed(sym, f"{o.status.value} {o.reason}")
            return
        fq = sum(f.qty for f in fills) or o.filled_qty
        fpx = (sum(f.qty * f.price for f in fills) / fq) if fills else o.avg_price
        fee = sum(f.fee for f in fills) if fills else fq * fpx * self.taker_bps / 1e4
        if mfills:                                        # 메이커 일부 + 시장가 나머지 → 합쳐 VWAP
            fpx = (fpx * fq + sum(f.qty * f.price for f in mfills)) / (fq + mq)
            fee += sum(f.fee for f in mfills)
        self._cancel_stop(sym)
        self._finalize(sym, fpx, fee, req["reason"] + ("(메이커 일부)" if mfills else ""), req.get("assumed_out"), cid)

    # ------------------------------------------------------------ 메이커 청산 (v3)
    def _place_maker(self, sym: str, ep: int) -> None:
        """마감 N초 전: bid1 에 reduceOnly GTX 매수. 거절·실패면 걸지 않는다 (엔진 청산 때 시장가)."""
        guard = self.brokers[sym]
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos or pos["t_sig_epoch"] != ep or pos["status"] != "open" or pos.get("exit_req") or pos.get("maker_cid"):
                return
            qty = pos["qty"]
        q = self.quote(sym)
        if not q or not q[0]:
            self.store.event({"event": "maker_skip", "symbol": sym, "t_sig": ep, "reason": "호가 없음"})
            return
        cid = self._cid(sym, ep, "m")
        try:
            o = guard.place_limit_gtx(Side.BUY, qty, q[0], cid, reduce_only=True)
        except Exception as e:
            self.store.event({"event": "maker_skip", "symbol": sym, "t_sig": ep, "reason": f"{type(e).__name__}: {str(e)[:120]}"})
            log.warning("%s 메이커 청산 주문 실패: %s", sym, e)
            return
        if not o.status.is_open and o.status is not Status.FILLED:
            self.store.event({"event": "maker_skip", "symbol": sym, "t_sig": ep, "reason": f"{o.status.value} {o.reason}"})
            return
        with self.lock:
            pos = self.state.positions.get(sym)
            if pos and pos["t_sig_epoch"] == ep:
                pos.update(maker_cid=cid, maker_px=o.price, maker_ts=self.now())
                self._save()
        self.store.event({"event": "maker_placed", "symbol": sym, "t_sig": ep, "price": o.price, "qty": qty, "cid": cid})

    def _poll_maker(self, sym: str, ep: int) -> None:
        """대기 중 메이커 청산이 다 체결됐으면 그걸로 손익 확정. 일부 체결은 엔진 청산 때 나머지를 시장가로."""
        guard = self.brokers[sym]
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos or pos["t_sig_epoch"] != ep or pos["status"] != "open" or not pos.get("maker_cid"):
                return
            cid, qty = pos["maker_cid"], pos["qty"]
        try:
            guard.poll()
        except Exception as e:
            log.warning("%s 메이커 청산 상태 조회 실패: %s", sym, e)
            return
        fills = [f for f in guard.fills() if f.client_id == cid]
        fq = round(sum(f.qty for f in fills), 8)
        if not fills or fq < qty - 1e-9:
            return
        self._cancel_stop(sym)
        self._finalize(sym, sum(f.qty * f.price for f in fills) / fq, sum(f.fee for f in fills), "5분(메이커)", None, cid)

    def _close_maker(self, sym: str, cid: str) -> list:
        """메이커 주문을 정리하고 그 체결 목록. 체결 확인 → 취소 → 다시 확인 (취소 직전 체결까지 잡는다)."""
        guard = self.brokers[sym]
        for step in ("poll", "cancel", "poll"):
            try:
                guard.poll() if step == "poll" else guard.cancel(cid)
            except (OrderRejected, KeyError):
                pass                                      # 이미 체결·취소됐다
            except Exception as e:
                self.alert(f"{sym} 메이커 청산 주문 정리 실패({step}) — {str(e)[:160]}. 남은 수량은 reduceOnly 로 청산합니다.")
        return [f for f in guard.fills() if f.client_id == cid]

    def _exit_failed(self, sym: str, why: str) -> None:
        with self.lock:
            pos = self.state.positions[sym]
            pos["status"] = "open"
            pos["exit_n"] = pos.get("exit_n", 0) + 1
            pos["exit_retry_at"] = self.now() + 2.0
            self._save()
        self.alert(f"{sym} 청산 주문 실패({pos['exit_n']}회) — {why}. 2초 뒤 다시 시도합니다.")

    def _cancel_stop(self, sym: str) -> None:
        pos = self.state.positions.get(sym)
        if not pos or not pos.get("stop_cid"):
            return
        try:
            self.brokers[sym].cancel(pos["stop_cid"])
        except (OrderRejected, KeyError):
            pass
        except Exception as e:
            self.alert(f"{sym} 비상 손절 주문 취소 실패 — {str(e)[:160]}. 거래소에서 확인해 주세요.")

    def _check_stop(self, sym: str, force: bool = False) -> bool:
        """비상 손절이 발동했으면 그 체결로 손익을 확정하고 True."""
        guard = self.brokers[sym]
        pos = self.state.positions.get(sym)
        if not pos or not pos.get("stop_cid") or pos["status"] not in ("open", "closing"):
            return False
        try:
            guard.poll()
            o = guard.get_order(pos["stop_cid"])
        except Exception as e:
            log.warning("%s 비상 손절 상태 조회 실패: %s", sym, e)
            return False
        fills = [f for f in guard.fills() if f.client_id == pos["stop_cid"]]
        if o.status is not Status.FILLED and not fills:
            return False
        if not fills and not force:
            return False
        fq = sum(f.qty for f in fills) or pos["qty"]
        fpx = (sum(f.qty * f.price for f in fills) / fq) if fills else (o.avg_price or pos["stop_px"])
        fee = sum(f.fee for f in fills) if fills else fq * fpx * self.taker_bps / 1e4
        self._finalize(sym, fpx, fee, "비상손절", None, pos["stop_cid"],
                       note=f"거래소 비상 손절(+{self.estop_pct}%)이 발동했습니다.")
        return True

    def _finalize(self, sym: str, exit_px: float, exit_fee: float, reason: str, assumed_out: Optional[float],
                  exit_cid: str, note: Optional[str] = None) -> None:
        now = self.now()
        with self.lock:
            pos = self.state.positions.get(sym)
            if not pos:
                return
            qty, epx = pos["qty"], pos["entry_px"]
            gross = (epx - exit_px) * qty
            fees = pos.get("entry_fee", 0.0) + exit_fee
            net = gross - fees
            pnl_bp = math.log(epx / exit_px) * 1e4 if exit_px > 0 else 0.0
            a_in = pos["assumed_in"]
            assumed_bp = math.log(a_in / assumed_out) * 1e4 if assumed_out else None
            slip_out = (exit_px - assumed_out) / assumed_out * 1e4 if assumed_out else None
            st = self.state
            st.cum_net += net
            st.cum_gross += gross
            st.cum_fees += fees
            st.n_trades += 1
            d = utc_day(now)
            r = st.realized.setdefault(d, {"net": 0.0, "n": 0, "win": 0, "loss": 0})
            r["net"] += net
            r["n"] += 1
            r["win" if net > 0 else "loss"] += 1
            st.positions[sym] = None
            trade = {"trade_id": f"{sym}-{pos['t_sig_epoch']}", "symbol": sym, "day": pos["day"],
                     "t_sig": pos["t_sig_epoch"], "entry_ts": pos.get("entry_ts"), "exit_ts": now, "side": -1,
                     "qty": qty, "entry_px": epx, "exit_px": exit_px, "assumed_in": a_in, "assumed_out": assumed_out,
                     "reason": reason, "gross_usd": gross, "fees_usd": fees, "net_usd": net, "pnl_bp": pnl_bp,
                     "assumed_bp": assumed_bp, "slip_in_bp": pos.get("slip_in_bp"), "slip_out_bp": slip_out,
                     "past_bp": pos.get("past_bp"), "ofi_ratio": pos.get("ofi_ratio"),
                     "entry_cid": pos.get("entry_cid"), "exit_cid": exit_cid, "spec_version": self.spec.version,
                     "source": pos.get("source"), "fee_estimated": pos.get("fee_estimated", False),
                     "maker_px": pos.get("maker_px")}
            hit = (not st.halted) and st.cum_net <= self.stop_line
            if hit:
                st.halted, st.halt_reason, st.halt_since = True, f"누적 실현 {money(st.cum_net)} ≤ {money(self.stop_line)}", now
            self._save()
        self.store.trade(trade)
        x = {"symbol": sym, "reason": reason, "qty": qty, "exit_px": exit_px, "entry_px": epx,
             "held_s": now - (pos.get("entry_ts") or now), "pnl_bp": pnl_bp, "gross": gross, "fees": fees, "net": net,
             "assumed_bp": assumed_bp, "note": note}
        self.store.event({"event": "exit", **x})
        self.notify(exit_text(x, self.positions(), self.account()))
        if hit:
            self.alert(f"⛔ 정지선 도달: 누적 실현 손익 {money(self.state.cum_net)} (정지선 {money(self.stop_line)}). "
                       f"신규 진입을 멈췄습니다. 열린 포지션은 규칙대로 청산합니다. 재개하려면 "
                       f"`python -m src.strategies.pastofi_crypto.runner --mode {self.mode} --reset-halt --note \"...\"` "
                       f"를 실행해 주세요.")

    # ------------------------------------------------------------ 매초
    def periodic(self) -> None:
        now = self.now()
        if self.resume_file.exists():
            self._consume_resume()
        for sym in self.spec.symbols:
            pos = self.state.positions.get(sym)
            if not pos or pos["status"] != "open":
                continue
            ep = pos["t_sig_epoch"]
            if pos.get("exit_req") and now >= pos.get("exit_retry_at", 0):
                pos["exit_retry_at"] = now + 5.0
                self._submit(sym, lambda s=sym, e=ep: self._do_exit(s, e))
                continue
            if self.maker_s and pos.get("source") == "engine" and not pos.get("exit_req"):
                if pos.get("maker_cid"):
                    self._submit(sym, lambda s=sym, e=ep: self._poll_maker(s, e))
                elif not pos.get("maker_tried") and pos["deadline_epoch"] - self.maker_s <= now < pos["deadline_epoch"]:
                    with self.lock:
                        pos["maker_tried"] = True                # 한 번만 건다 (거절·실패여도 다시 걸지 않는다)
                        self._save()
                    self._submit(sym, lambda s=sym, e=ep: self._place_maker(s, e))
            if pos.get("source") == "recovered" or now > pos["deadline_epoch"] + 10:
                why = self._soft_exit(sym, pos, now)
                if why:
                    q = self.quote(sym)
                    with self.lock:
                        pos["exit_req"] = {"reason": why, "assumed_out": q[1] if q else None, "t_out": now}
                        pos["exit_retry_at"] = now + 5.0
                        self._save()
                    self._submit(sym, lambda s=sym, e=ep: self._do_exit(s, e))
                    continue
            if pos.get("stop_cid") and now >= self._next_poll[sym]:
                self._next_poll[sym] = now + 5.0
                self._submit(sym, lambda s=sym: self._check_stop(s))

    def _soft_exit(self, sym: str, pos: dict, now: float) -> Optional[str]:
        """엔진 밖에서 관리하는 포지션(재시작 복구 등): ask1 로 익절·손절, 마감 지나면 청산."""
        if now >= pos["deadline_epoch"]:
            return "5분(재시작)" if pos.get("source") == "recovered" else "5분(보완)"
        q = self.quote(sym)
        if q:
            if q[1] <= pos["tp_px"]:
                return "익절(재시작)"
            if q[1] >= pos["sl_px"]:
                return "손절(재시작)"
        return None

    # ------------------------------------------------------------ 정지선 수동 재개
    def _consume_resume(self) -> None:
        try:
            req = json.loads(self.resume_file.read_text())
        except Exception:
            req = {}
        self.resume_file.unlink(missing_ok=True)
        with self.lock:
            was = self.state.halted
            self.state.halted, self.state.halt_reason, self.state.halt_since = False, "", None
            if req.get("reset_cum"):
                self.state.cum_net = self.state.cum_gross = self.state.cum_fees = 0.0
            self._save()
        for g in self.brokers.values():
            if g.state.status != "active":
                try:
                    g.reset_halt(req.get("note", "수동 재개"))
                except Exception as e:
                    self.alert(f"{g.symbol} 리스크 해제 실패 — {e}")
        self.alert(f"정지선 수동 재개{'(누적 초기화)' if req.get('reset_cum') else ''}: {req.get('note', '')} "
                   f"(이전 상태 {'정지' if was else '정상'}). 누적 실현 {money(self.state.cum_net)}.")

    # ------------------------------------------------------------ 재시작 복구
    def recover(self, foreign_check: bool) -> list[str]:
        """거래소(페이퍼는 시뮬 상태)와 장부 대조. 고친 내용 목록. 시작하면 안 되면 StartRefused."""
        notes = []
        now = self.now()
        for sym in self.spec.symbols:
            g = self.brokers[sym]
            actual = g.position().qty
            pos = self.state.positions.get(sym)
            if pos and pos["status"] in ("opening",) and abs(actual) < 1e-12:
                notes.append(f"{sym}: 진입 주문 중 종료 — 거래소 포지션 없음, 장부 정리")
                self.state.positions[sym] = pos = None
            if pos is None:
                if abs(actual) > 1e-12:
                    raise StartRefused(f"{sym}: 이 전략 장부에 없는 포지션 {actual} — 다른 프로그램(예: overnight)이 "
                                       f"남긴 것일 수 있습니다. 정리한 뒤 시작해 주세요.")
            else:
                if abs(actual + pos["qty"]) > 1e-9 and abs(actual) > 1e-12:
                    raise StartRefused(f"{sym}: 장부 숏 {pos['qty']} ≠ 거래소 {actual}")
                if abs(actual) < 1e-12:                  # 꺼져 있는 동안 비상 손절이 났거나 밖에서 닫혔다
                    if not self._recover_closed(sym, pos):
                        notes.append(f"{sym}: 거래소 포지션 없음 — 청산 체결을 못 찾아 진입가로 닫음(확인 필요)")
                    continue
                if not pos.get("entry_px"):
                    p = g.position()
                    pos["entry_px"] = p.entry_price or pos["assumed_in"]
                    pos.setdefault("entry_fee", pos["qty"] * pos["entry_px"] * self.taker_bps / 1e4)
                    pos.setdefault("entry_ts", now)
                pos["status"], pos["source"] = "open", "recovered"
                pos.pop("exit_req", None)
                notes.append(f"{sym}: 숏 {pos['qty']} 이어서 관리 (마감 {_dt(pos['deadline_epoch']):%H:%M:%S} UTC"
                             + (", 이미 지남 → 즉시 청산" if now >= pos["deadline_epoch"] else "") + ")")
            # 미체결 주문
            mine = {p.get("stop_cid") for p in [self.state.positions.get(sym)] if p}
            for o in g.open_orders():
                if not o.client_id.startswith(self.prefix):
                    if foreign_check:
                        raise StartRefused(f"{sym}: 이 전략이 내지 않은 미체결 주문 {o.client_id} ({o.type.value} {o.side.value})")
                    continue
                if o.client_id not in mine:
                    try:
                        g.cancel(o.client_id)
                        notes.append(f"{sym}: 남은 주문 {o.client_id} 취소")
                    except Exception as e:
                        notes.append(f"{sym}: 남은 주문 {o.client_id} 취소 실패 {e}")
        self._save()
        return notes

    def _recover_closed(self, sym: str, pos: dict) -> bool:
        g = self.brokers[sym]
        since = _dt(pos.get("entry_ts") or pos["t_sig_epoch"])
        try:
            fills = [f for f in g.fills_since(since) if f.side is Side.BUY]
        except Exception:
            fills = []
        if not fills:
            fills = [f for f in g.fills() if f.side is Side.BUY and f.ts >= since]
        if not pos.get("entry_px"):
            self.state.positions[sym] = None
            return False
        if fills:
            q = sum(f.qty for f in fills)
            self._finalize(sym, sum(f.qty * f.price for f in fills) / q, sum(f.fee for f in fills),
                           "비상손절(꺼진 동안)", None, fills[-1].client_id,
                           note="프로세스가 꺼져 있는 동안 포지션이 닫혔습니다.")
            return True
        self._finalize(sym, pos["entry_px"], 0.0, "확인 필요(꺼진 동안 닫힘)", None, "",
                       note="⚠️ 꺼져 있는 동안 포지션이 사라졌는데 청산 체결을 찾지 못했습니다. 거래소를 확인해 주세요.")
        return False
