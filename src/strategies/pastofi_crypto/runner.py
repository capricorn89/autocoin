"""pastofi 코인판 실행 프로세스. 페이퍼·라이브 같은 코드 경로, 브로커만 다르다.

    python -m src.strategies.pastofi_crypto.runner                          # paper (기본, 실주문 없음·API 키 안 씀)
    python -m src.strategies.pastofi_crypto.runner --mode live --confirm-live
    python -m src.strategies.pastofi_crypto.runner --status [--mode live]
    python -m src.strategies.pastofi_crypto.runner --reset-halt --note "확인함" [--reset-cum] [--mode live]
    python -m src.strategies.pastofi_crypto.runner --test-notify             # 텔레그램 테스트 메시지

구성
    WS: 수집기 코드(src/exchange/ws.py OrderBookCollector)를 새 연결로 띄우고 싱크만 바꾼다 (수집기 프로세스와 별개).
        싱크가 book_top·trade 를 SymbolFeed(엔진)와 페이퍼 호가 뷰에 넣고, 갭·연결 이벤트로 데이터 상태를 센다.
    시계: 거래소 시각 = 벽시계 - 오프셋(/fapi/v1/time, 10분마다). 초 T 판단은 거래소 시각 T+0.3초.
    상태: data/pastofi/<mode>/ — state.json(장부), live.json(웹용, 매초), trades/signals/events.jsonl, paper_*.json, risk_*.json, KILL
    매일 UTC 00:20 에 전날 일치 대조(reconcile)를 하위 프로세스로 돌린다. 페이퍼 7일이 지나면 요약을 알린다(멈추지 않는다).
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import os
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from .feed import DAY, Signal, SymbolFeed
from .notify import SHORT, AsyncNotifier, money
from .spec import Spec, SpecError, SymbolFilters, sized
from .store import Store
from .trader import StartRefused, Trader

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]
STATE_ROOT = REPO / "data" / "pastofi"
WEB_URL = "http://100.121.82.67:8790"
RECONCILE_AFTER_S = 1200          # UTC 자정 + 20분 (수집기 DB 적재가 끝난 뒤)


def _utc(ts: float, fmt: str = "%Y-%m-%d %H:%M:%S") -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime(fmt)


# ---------------------------------------------------------------- 데이터 상태
class Health:
    """종목별 데이터 공백·지연. 시각은 모두 거래소 시각(epoch 초)."""

    def __init__(self, symbols: list[str], now: float):
        self.gaps: dict[str, list[list[float]]] = {s: [[0.0, now]] for s in symbols}   # 프로세스 시작 = 공백
        self.open_gap: dict[str, Optional[float]] = {s: None for s in symbols}
        self.depth_lat: dict[str, Optional[float]] = {s: None for s in symbols}
        self.trade_lat: dict[str, Optional[float]] = {s: None for s in symbols}
        self.last_depth: dict[str, Optional[float]] = {s: None for s in symbols}
        self.last_trade: dict[str, Optional[float]] = {s: None for s in symbols}
        self.connected = {"depth": False, "trade": False}
        self.stream_down_since: dict[str, Optional[float]] = {"depth": now, "trade": now}
        self.last_msg: dict[str, float] = {"depth": now, "trade": now}
        self.events: list[dict] = []

    def start_gap(self, sym: str, t: float, why: str) -> None:
        if self.open_gap[sym] is None:
            self.open_gap[sym] = t
            self.events.append({"t": t, "symbol": sym, "what": f"공백 시작: {why}"})

    def end_gap(self, sym: str, t: float) -> None:
        s = self.open_gap[sym]
        if s is not None:
            self.gaps[sym].append([s, t])
            self.gaps[sym] = self.gaps[sym][-50:]
            self.open_gap[sym] = None
            self.events.append({"t": t, "symbol": sym, "what": f"공백 끝 ({t - s:.1f}s)"})
        self.events = self.events[-200:]

    def point_gap(self, sym: str, t: float, why: str) -> None:
        self.gaps[sym].append([t, t])
        self.events.append({"t": t, "symbol": sym, "what": why})

    def last_gap_end(self, sym: str) -> float:
        return max(g[1] for g in self.gaps[sym]) if self.gaps[sym] else 0.0


class StrategySink:
    """OrderBookCollector 싱크 — 레코드를 엔진·호가 뷰·데이터 상태로 나눈다."""

    def __init__(self, app: "App"):
        self.app = app

    def write(self, r: dict) -> None:
        a = self.app
        kind, sym = r.get("kind"), r.get("symbol")
        recv = r.get("recv_ts")
        now_ex = (recv / 1000 - a.offset_s) if recv else a.ex_now()
        if kind == "book_top":
            a.views[sym].on_book_top(r)
            a.feeds[sym].on_book_top(r)
            h = a.health
            h.depth_lat[sym] = (now_ex - r["E"] / 1000) * 1000 if r.get("E") else None
            h.last_depth[sym] = now_ex
            h.last_msg["depth"] = now_ex
            if h.open_gap[sym] is not None and a.feeds_synced(sym):
                h.end_gap(sym, now_ex)
        elif kind == "trade":
            if sym in a.views:
                a.views[sym].on_trade(r)
                a.feeds[sym].on_trade(r)
                a.health.trade_lat[sym] = (now_ex - r["E"] / 1000) * 1000 if r.get("E") else None
                a.health.last_trade[sym] = now_ex
                a.health.last_msg["trade"] = now_ex
        elif kind == "depth":
            a.health.last_msg["depth"] = now_ex
        elif kind == "gap":
            if r.get("stream") == "depth":
                a.health.start_gap(sym, now_ex, "depth 시퀀스 갭")
            else:
                a.health.point_gap(sym, now_ex, f"aggTrade id {r.get('detail')} {r.get('from_id')}~{r.get('to_id')}")
            a.store.event({"event": "gap", **{k: v for k, v in r.items() if k != "kind"}})
        elif kind == "conn":
            stream, ev = r.get("stream"), r.get("event")
            a.health.connected[stream] = ev == "connected"
            if ev == "connected":
                a.health.stream_down_since[stream] = None
                if stream == "trade":
                    for s in a.symbols:
                        a.health.point_gap(s, now_ex, "체결 연결 다시 붙음")
            else:
                a.health.stream_down_since[stream] = a.health.stream_down_since[stream] or now_ex
                for s in a.symbols:
                    a.health.start_gap(s, now_ex, f"{stream} 연결 끊김") if stream == "depth" else \
                        a.health.point_gap(s, now_ex, "체결 연결 끊김")
            a.store.event({"event": "conn", "stream": stream, "what": ev, "reason": r.get("reason", "")})

    def flush(self) -> None:
        pass

    def close(self) -> None:
        pass


# ---------------------------------------------------------------- 실행
class App:
    def __init__(self, spec: Spec, mode: str, trader: Trader, store: Store, views: dict, notify: AsyncNotifier,
                 qty_note: str, rest=None, live_client=None):
        self.spec, self.mode, self.trader, self.store, self.views = spec, mode, trader, store, views
        self.notify = notify
        self.qty_note = qty_note
        self.symbols = spec.symbols
        self.dir = trader.dir
        self.offset_s = 0.0
        self.offset_ms: Optional[float] = None
        self.offset_at: Optional[float] = None
        self.delay = float(spec.raw["timing"]["decision_delay_s"])
        self.g = spec.raw["gate"]
        self.lag = 0.0
        self.started = self.ex_now()
        self.health = Health(self.symbols, self.started)
        self.feeds = {s: SymbolFeed(spec, s, trader.on_entry, trader.on_exit) for s in self.symbols}
        trader.gate = self.gate
        trader.quote = self.quote
        self.collector = None
        self.rest = rest
        self.live_client = live_client
        self.wallet: Optional[float] = None
        self.ex_warn: list[str] = []
        self.outage_alerted: dict[str, bool] = {"depth": False, "trade": False}
        self.reconcile_pending: set[int] = set()
        self.reconcile_proc: Optional[asyncio.subprocess.Process] = None

    # ------------------------------------------------------------ 시계·상태
    def ex_now(self) -> float:
        return time.time() - self.offset_s

    def feeds_synced(self, sym: str) -> bool:
        if self.collector is None:
            return True
        b = self.collector.books.get(sym)
        return bool(b and b.synced and b.book.ready)

    def quote(self, sym: str) -> Optional[tuple]:
        try:
            b = self.views[sym].book()
            return b.best_bid, b.best_ask
        except Exception:
            return None

    def gate(self, sym: str, sig: Signal) -> Optional[str]:
        g, h = self.g, self.health
        T = sig.epoch_sig
        if self.lag > float(g["max_decision_lag_s"]):
            return f"판단 지연 {self.lag:.1f}s"
        lim = float(self.spec.raw["timing"]["max_clock_offset_ms"])
        if self.offset_ms is None:
            return "시계 오프셋 측정 전"
        if abs(self.offset_ms) > lim:
            return f"시계 오프셋 {self.offset_ms:.0f}ms > {lim:.0f}ms"
        if not self.feeds_synced(sym) or h.open_gap[sym] is not None:
            return "데이터 공백 (depth 재동기화 중)"
        if h.last_gap_end(sym) > T - float(g["gap_window_s"]):
            return f"피처 창 안 데이터 공백 (마지막 공백 끝 {_utc(h.last_gap_end(sym), '%H:%M:%S')})"
        now = self.ex_now()
        if h.last_depth[sym] is None or now - h.last_depth[sym] > float(g["max_depth_silence_s"]):
            return "depth 끊김"
        if (h.depth_lat[sym] or 0) > float(g["max_depth_latency_ms"]):
            return f"depth 지연 {h.depth_lat[sym]:.0f}ms"
        if (h.trade_lat[sym] or 0) > float(g["max_trade_latency_ms"]):
            return f"체결 지연 {h.trade_lat[sym]:.0f}ms"
        if not h.connected["trade"]:
            return "체결 연결 끊김"
        return None

    # ------------------------------------------------------------ 루프
    async def run(self) -> None:
        from ...exchange.rest import FuturesRestClient
        from ...exchange.ws import CollectorConfig, OrderBookCollector
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        cfg = CollectorConfig(symbols=self.symbols, depth_speed=self.spec.raw["input"]["depth_speed"])
        self.collector = OrderBookCollector(cfg, StrategySink(self), rest=self.rest or FuturesRestClient())
        await self._measure_offset()
        self.started = self.ex_now()
        self.health = Health(self.symbols, self.started)
        self._schedule_missing_reconciles()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self.collector.run(stop))
            tg.create_task(self._clock_loop(stop))
            tg.create_task(self._offset_loop(stop))
            if self.mode == "live":
                tg.create_task(self._exchange_loop(stop))

    async def _measure_offset(self) -> None:
        from ...exchange.rest import FuturesRestClient, measure_clock_offset
        try:
            r = await asyncio.to_thread(measure_clock_offset, self.rest or FuturesRestClient(), 5)
            self.offset_ms, self.offset_at = r["offset_ms"], time.time()
            self.offset_s = r["offset_ms"] / 1000
            self.store.event({"event": "clock", **r})
        except Exception as e:
            log.warning("시계 오프셋 측정 실패: %s", e)

    async def _offset_loop(self, stop: asyncio.Event) -> None:
        every = float(self.spec.raw["timing"]["clock_offset_every_s"])
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=every)
            except TimeoutError:
                await self._measure_offset()

    async def _exchange_loop(self, stop: asyncio.Event) -> None:
        """라이브: 지갑 잔고·포지션을 30초마다 거래소에서 읽어 웹·알림에 쓰고, 장부와 다르면 경고."""
        while not stop.is_set():
            try:
                acc = await asyncio.to_thread(self.live_client.get, "/fapi/v2/account", {}, True)
                self.wallet = float(acc["totalWalletBalance"])
                rows = await asyncio.to_thread(self.live_client.get, "/fapi/v3/positionRisk", {}, True)
                ex = {s: (0.0, 0.0) for s in self.symbols}
                for r in rows:
                    if r["symbol"] in ex and float(r["positionAmt"]) != 0:
                        ex[r["symbol"]] = (float(r["positionAmt"]), float(r["entryPrice"]))
                self.trader.ex_positions = ex
                warn = []
                for s in self.symbols:
                    pos = self.trader.state.positions.get(s)
                    want = -pos["qty"] if pos and pos.get("status") in ("open", "closing") else 0.0
                    if pos and pos.get("status") == "opening":
                        continue
                    if abs(ex[s][0] - want) > 1e-9:
                        warn.append(f"{s} 장부 {want:g} ≠ 거래소 {ex[s][0]:g}")
                st = self.trader.state
                if st.live_wallet_start is None:
                    st.live_wallet_start = self.wallet - st.cum_net
                    self.trader._save()
                exp = st.live_wallet_start + st.cum_net
                if abs(self.wallet - exp) > 0.05:
                    warn.append(f"지갑 {self.wallet:.4f} ≠ 장부 기대 {exp:.4f} (펀딩·수동 거래 확인)")
                if warn and warn != self.ex_warn:
                    self.trader.alert("⚠️ 장부와 거래소가 다릅니다: " + " / ".join(warn))
                self.ex_warn = warn
            except Exception as e:
                log.warning("거래소 조회 실패: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), timeout=30)
            except TimeoutError:
                pass

    async def _clock_loop(self, stop: asyncio.Event) -> None:
        last = math.floor(self.ex_now() - self.delay)
        last_write = 0.0
        while not stop.is_set():
            now = self.ex_now()
            target = math.floor(now - self.delay)
            while last < target:
                last += 1
                day, T = divmod(last, DAY)
                self.lag = now - (last + self.delay)
                for f in self.feeds.values():
                    f.tick(day, T)
                if T == DAY - 1:
                    for f in self.feeds.values():
                        f.finish_day(day)
                    self.reconcile_pending.add(day)
            try:
                self.trader.periodic()
            except Exception:
                log.exception("periodic 오류")
            if time.time() - last_write >= 1.0:
                last_write = time.time()
                self._write_live(last)
                self._watch()
                await self._maybe_reconcile()
            await asyncio.sleep(max(0.005, (last + 1 + self.delay) - self.ex_now()))
        self._write_live(last)

    def _watch(self) -> None:
        """WS 끊김 3분 알림, 7일 요약."""
        now = self.ex_now()
        lim = float(self.spec.raw["alerts"]["ws_outage_s"])
        for stream in ("depth", "trade"):
            silent = now - self.health.last_msg[stream]
            if stream == "trade":       # 체결은 조용할 수 있다 — 연결 끊김만 본다
                down = self.health.stream_down_since[stream]
                silent = now - down if down else 0.0
            if silent >= lim and not self.outage_alerted[stream]:
                self.outage_alerted[stream] = True
                self.trader.alert(f"⚠️ WS({stream})가 {silent / 60:.0f}분 넘게 끊겼습니다. 그동안 신규 진입은 하지 않고, "
                                  f"열린 포지션은 시간 청산을 계속합니다.")
            elif silent < 5 and self.outage_alerted[stream]:
                self.outage_alerted[stream] = False
                self.trader.alert(f"WS({stream})가 다시 연결됐습니다.")
        st = self.trader.state
        if self.mode == "paper" and not st.trial_summary_sent and time.time() >= st.trial_end:
            st.trial_summary_sent = True
            self.trader._save()
            asyncio.get_running_loop().run_in_executor(None, self._trial_summary)

    def _trial_summary(self) -> None:
        try:
            from .report import build_report, write_report
            rep = build_report(self.mode, self.dir)
            path = write_report(rep, self.dir)
            self.trader.alert(f"📋 페이퍼 {self.spec.raw['paper']['trial_days']}일 요약\n{rep['text']}\n"
                              f"리포트: {path}\n웹: {WEB_URL} · 페이퍼는 멈추지 않고 계속 돕니다. 라이브 전환은 따로 지시해 주세요.")
        except Exception as e:
            log.exception("7일 요약 실패")
            self.trader.alert(f"7일 요약을 만들지 못했습니다: {e}")

    def _schedule_missing_reconciles(self) -> None:
        rdir = self.dir / "reconcile"
        first = int(self.trader.state.started_at // DAY)
        today = int(self.ex_now() // DAY)
        for d in range(first, today):
            if not (rdir / f"{_utc(d * DAY, '%Y-%m-%d')}.json").exists():
                self.reconcile_pending.add(d)

    async def _maybe_reconcile(self) -> None:
        if self.reconcile_proc is not None:
            if self.reconcile_proc.returncode is None:
                return
            self.reconcile_proc = None
        ready = sorted(d for d in self.reconcile_pending if self.ex_now() >= (d + 1) * DAY + RECONCILE_AFTER_S)
        if not ready:
            return
        d = ready[0]
        self.reconcile_pending.discard(d)
        day = _utc(d * DAY, "%Y-%m-%d")
        log.info("일치 대조 시작 %s", day)
        self.reconcile_proc = await asyncio.create_subprocess_exec(
            "nice", "-n", "10", sys.executable, "-m", "src.strategies.pastofi_crypto.reconcile",
            "--mode", self.mode, "--date", day, "--notify", cwd=str(REPO),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)

    def _write_live(self, last_T: int) -> None:
        try:
            day, T = divmod(last_T, DAY)
            st = self.trader.state
            now = self.ex_now()
            syms = {}
            for s in self.symbols:
                q = self.quote(s)
                snap = self.feeds[s].snapshot(day, T)
                pos = st.positions.get(s)
                h = self.health
                syms[s] = {
                    "bid": q[0] if q else None, "ask": q[1] if q else None,
                    "mid": (q[0] + q[1]) / 2 if q else None, **snap,
                    "move_bp": self.trader.p.move_bp, "ofi_th": -self.trader.p.ofi_th,
                    "entries_today": st.entries.get(_utc(now, "%Y-%m-%d"), {}).get(s, 0),
                    "gate": self.gate(s, Signal(s, day, T, 0, 0, 0, {})),
                    "depth_latency_ms": h.depth_lat[s], "trade_latency_ms": h.trade_lat[s],
                    "last_depth_age_s": (now - h.last_depth[s]) if h.last_depth[s] else None,
                    "synced": self.feeds_synced(s), "late_trades": self.feeds[s].late_trades(day),
                    "position": ({k: pos.get(k) for k in ("status", "qty", "entry_px", "assumed_in", "tp_px", "sl_px",
                                                           "deadline_epoch", "t_sig_epoch", "source", "stop_px")}
                                 if pos else None),
                }
                if pos and pos.get("entry_px") and q:
                    syms[s]["position"]["upnl_bp"] = math.log(pos["entry_px"] / q[1]) * 1e4
                    syms[s]["position"]["upnl_usd"] = (pos["entry_px"] - q[1]) * pos["qty"]
                    syms[s]["position"]["remaining_s"] = pos["deadline_epoch"] - now
                if self.trader.ex_positions is not None:
                    syms[s]["exchange_qty"] = self.trader.ex_positions.get(s, (0.0, 0.0))[0]
            live = {
                "mode": self.mode, "spec_version": self.spec.version, "spec_sha": self.spec.sha256[:12],
                "pid": os.getpid(), "heartbeat": time.time(), "ex_now": now, "decision_T": last_T,
                "process_started": self.started, "clock_offset_ms": self.offset_ms, "lag_s": self.lag,
                "trial": {"start": st.started_at, "end": st.trial_end, "days": self.spec.raw["paper"]["trial_days"],
                          "summary_sent": st.trial_summary_sent},
                "ws": {k: v for k, v in self.health.connected.items()},
                "account": self.trader.account(), "wallet": self.wallet, "exchange_warnings": self.ex_warn,
                "symbols": syms, "health_events": self.health.events[-20:],
                "qty": self.qty_note, "kill": (self.dir / "KILL").exists(),
            }
            tmp = self.dir / "live.tmp"
            tmp.write_text(json.dumps(live, ensure_ascii=False, default=str))
            tmp.replace(self.dir / "live.json")
        except Exception:
            log.exception("live.json 쓰기 실패")


# ---------------------------------------------------------------- 조립
def _risk_guard(broker, spec: Spec, sym: str, state_dir: Path, alert):
    from ...execution.risk import RiskGuard, RiskLimits
    limits = RiskLimits(max_abs_position=float(spec.raw["sizing"]["max_qty"][sym]),
                        test_stop_cumulative_usd=-1e12,               # 정지선은 4종목 합으로 trader 가 본다
                        pause_after_consecutive_losses=10 ** 9,
                        max_clock_offset_ms=float(spec.raw["timing"]["max_clock_offset_ms"]),
                        max_data_stale_s=1e9,
                        max_consecutive_rejects=int(spec.raw["risk"]["max_consecutive_rejects"]))
    return RiskGuard(broker, limits, state_dir / f"risk_{sym}.json", state_dir / "KILL", alert=alert)


def agents_running(labels: list[str]) -> list[str]:
    r = subprocess.run(["launchctl", "list"], capture_output=True, text=True)
    names = {line.split("\t")[-1].strip() for line in r.stdout.splitlines()[1:]}
    return [x for x in labels if x in names]


def build(mode: str, confirm_live: bool = False, state_root: Path = STATE_ROOT,
          spec_path: Optional[Path] = None) -> App:
    from ...exchange.rest import FuturesRestClient
    from ...execution.types import FeeSchedule
    from ...notify import TelegramNotifier
    from .brokers import LiveMarketView
    spec = Spec.load(spec_path) if spec_path else Spec.load()
    state_dir = state_root / mode
    state_dir.mkdir(parents=True, exist_ok=True)
    tg = TelegramNotifier.from_env()
    if tg is not None:
        tg.prefix = f"[pastofi {mode.upper()}]"
    notify = AsyncNotifier(tg.send if tg else None)
    public = FuturesRestClient()
    info = public.get("/fapi/v1/exchangeInfo")
    filters = {s: SymbolFilters.from_exchange_info(info, s) for s in spec.symbols}
    views = {s: LiveMarketView(s) for s in spec.symbols}
    fees = FeeSchedule(float(spec.raw["fees_bps"]["maker"]), float(spec.raw["fees_bps"]["taker"]))
    store = Store(mode, state_dir)
    live_client = None
    if mode == "paper":
        from ...execution.paper import PaperExchange
        inner = {s: PaperExchange(s, views[s], fees, state_file=state_dir / f"paper_{s}.json") for s in spec.symbols}
    elif mode == "live":
        if not confirm_live:
            raise SystemExit("live 모드는 --confirm-live 가 필요합니다.")
        from ...exchange.auth import BinanceCredentials
        from .brokers import PastofiLiveBroker
        live_client = FuturesRestClient(credentials=BinanceCredentials.from_env())
        inner = {s: PastofiLiveBroker(s, fees, client=live_client) for s in spec.symbols}
    else:
        raise SystemExit(f"알 수 없는 모드 {mode!r}")
    guards = {s: _risk_guard(inner[s], spec, s, state_dir, notify) for s in spec.symbols}
    execs = {s: ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ord-{SHORT[s]}") for s in spec.symbols}

    def submit(sym, fn):
        def run():
            try:
                fn()
            except Exception as e:
                log.exception("%s 주문 작업 오류", sym)
                notify(f"{sym} 주문 작업 오류: {type(e).__name__}: {str(e)[:200]}")
        execs[sym].submit(run)

    trader = Trader(spec, mode, guards, filters, store, state_dir, notify=notify, submit=submit)
    trader.ex_positions = None
    # 시작 시 수량 (공개 호가 기준) — 상한을 넘으면 실행 거부
    qty = {}
    bids = {s: float(public.get("/fapi/v1/ticker/bookTicker", {"symbol": s})["bidPrice"]) for s in spec.symbols}
    for s in spec.symbols:
        bid = bids[s]
        q = sized(spec, s, bids, filters)               # SpecError → 시작 거부
        f = filters[s]
        qty[s] = f"{q:g} (step {f.step:g}, MIN_NOTIONAL ${f.min_notional:g}, 가격 {bid:g} → 명목 ${q * bid:.2f})"
    qty_note = " · ".join(f"{SHORT[s]} {v.split(' ')[0]}" for s, v in qty.items())
    store.event({"event": "start", "spec_version": spec.version, "spec_sha256": spec.sha256, "qty": qty,
                 "filters": {s: vars(f) for s, f in filters.items()}})
    for s, v in qty.items():
        log.info("수량 %s: %s", s, v)
    if mode == "live":
        _live_preflight(spec, inner, live_client, store)
    notes = trader.recover(foreign_check=(mode == "live"))
    app = App(spec, mode, trader, store, views, notify, qty_note, rest=public, live_client=live_client)
    trader.wallet = lambda: app.wallet
    for n in notes:
        log.warning("복구: %s", n)
    if notes:
        trader.alert("재시작 복구:\n" + "\n".join(notes))
    try:
        store.sync_all()
    except Exception as e:
        log.warning("DB 동기화 실패 (jsonl 은 그대로): %s", e)
    return app


def _live_preflight(spec: Spec, brokers: dict, client, store: Store) -> None:
    running = agents_running(spec.raw["live"]["refuse_if_agents"])
    if running:
        raise StartRefused(f"다른 실주문 에이전트가 떠 있습니다: {running} — 같은 계정(one-way)에서 포지션이 상계됩니다.")
    bad = []
    fees = {}
    for s, b in brokers.items():
        bad += [f"{s} {x}" for x in b.verify_account(spec.raw["live"]["account"])]
        fr = client.get("/fapi/v1/commissionRate", {"symbol": s}, signed=True)
        fees[s] = {"maker_bps": float(fr["makerCommissionRate"]) * 1e4, "taker_bps": float(fr["takerCommissionRate"]) * 1e4}
    store.event({"event": "commission", "fees": fees})
    if bad:
        raise StartRefused("계정 설정이 스펙과 다릅니다: " + "; ".join(bad))


# ---------------------------------------------------------------- CLI
def status_text(mode: str, state_root: Path = STATE_ROOT) -> str:
    d = state_root / mode
    if not (d / "live.json").exists():
        return f"pastofi {mode}: 상태 파일 없음 (아직 시작 전)"
    lv = json.loads((d / "live.json").read_text())
    a = lv["account"]
    age = time.time() - lv["heartbeat"]
    lines = [f"■ pastofi {mode}  {lv['spec_version']} ({lv['spec_sha']})  하트비트 {age:.0f}초 전"
             + ("  ⚠️ 멈춤?" if age > 10 else ""),
             f"  잔고 {money(a['balance'], plus=False)} · 오늘 {money(a['today'])} · 누적 {money(a['cum'])} "
             f"(정지선 {money(a['stop'])}) · {'⛔ 정지 ' + a['halt_reason'] if a['halted'] else '정상'}",
             f"  페이퍼 {_utc(lv['trial']['start'], '%m/%d %H:%M')} ~ {_utc(lv['trial']['end'], '%m/%d %H:%M')} UTC",
             f"  수량 {lv['qty']} · 시계 오프셋 {lv['clock_offset_ms']}ms · WS {lv['ws']}"]
    for s, v in lv["symbols"].items():
        p = v.get("position")
        lines.append(f"  {s}: mid {v.get('mid')} past {v.get('past_bp') and round(v['past_bp'], 1)}bp "
                     f"OFI비율 {v.get('ofi_ratio') and round(v['ofi_ratio'], 2)} 오늘 {v['entries_today']}건 "
                     f"{'포지션 ' + str(p['qty']) + ' @ ' + str(p['entry_px']) if p else ''} {('게이트: ' + v['gate']) if v['gate'] else ''}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    from ...logging_setup import setup_logging
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.runner")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--confirm-live", action="store_true")
    ap.add_argument("--status", action="store_true")
    ap.add_argument("--reset-halt", action="store_true", help="정지선 수동 재개 (실행 중이면 1초 안에, 아니면 다음 시작 때)")
    ap.add_argument("--reset-cum", action="store_true", help="--reset-halt 와 함께: 누적 손익도 0으로")
    ap.add_argument("--note", default="")
    ap.add_argument("--test-notify", action="store_true")
    ap.add_argument("--state-root", type=Path, default=STATE_ROOT)
    args = ap.parse_args(argv)
    if args.status:
        print(status_text(args.mode, args.state_root))
        return
    if args.reset_halt:
        d = args.state_root / args.mode
        d.mkdir(parents=True, exist_ok=True)
        (d / "resume_request.json").write_text(json.dumps({"note": args.note or "수동 재개", "reset_cum": args.reset_cum,
                                                           "requested_at": time.time()}, ensure_ascii=False))
        print(f"재개 요청을 남겼습니다: {d / 'resume_request.json'} (실행 중이면 1초 안에 적용, 아니면 다음 시작 때)")
        return
    if args.test_notify:
        from ...notify import TelegramNotifier
        tg = TelegramNotifier.from_env()
        if tg is None:
            raise SystemExit("TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID 가 ~/.config/autocoin/.env 에 없습니다.")
        tg.prefix = f"[pastofi {args.mode.upper()}]"
        ok = tg.send("테스트 메시지입니다. 진입·청산·경고 알림이 이 대화방으로 옵니다.")
        print("전송 성공" if ok else "전송 실패")
        raise SystemExit(0 if ok else 1)
    setup_logging("INFO", REPO / "logs" / f"pastofi.{args.mode}.log", console_level="WARNING")
    if args.mode == "live" and not args.confirm_live:
        raise SystemExit("live 모드는 --confirm-live 가 필요합니다.")
    notify_fallback = None
    try:
        app = build(args.mode, args.confirm_live, args.state_root)
    except (StartRefused, SpecError) as e:
        from ...notify import TelegramNotifier
        tg = TelegramNotifier.from_env()
        msg = f"시작을 거부했습니다 — {e}"
        log.error(msg)
        if tg:
            tg.prefix = f"[pastofi {args.mode.upper()}]"
            tg.send(msg)
        print(msg, file=sys.stderr)
        raise SystemExit(0)             # launchd 가 같은 이유로 계속 재시작하지 않게 (KeepAlive SuccessfulExit=false)
    st = app.trader.state
    day_n = int((time.time() - st.started_at) // DAY) + 1
    app.notify(f"시작 — {app.spec.version} ({app.spec.sha256[:12]}), 종목 {', '.join(SHORT[s] for s in app.symbols)}\n"
               f"수량 {app.qty_note}\n"
               + (f"페이퍼 {day_n}/{app.spec.raw['paper']['trial_days']}일째 (종료 예정 {_utc(st.trial_end, '%m/%d %H:%M')} UTC, "
                  f"이후에도 계속 돕니다)\n" if args.mode == "paper" else "")
               + f"정지선 {money(app.trader.stop_line)} · 누적 {money(st.cum_net)} · 웹 {WEB_URL}")
    _obsidian(f"pastofi {args.mode} 시작 — {app.spec.version}, 수량 {app.qty_note}, 누적 {money(st.cum_net)}")
    try:
        asyncio.run(app.run())
    except Exception as e:
        app.store.event({"event": "stop", "error": f"{type(e).__name__}: {str(e)[:200]}"})
        app.trader.alert(f"예외 종료 ({type(e).__name__}: {str(e)[:200]}) — launchd 가 다시 띄우고 장부를 복구합니다.")
        app.notify.drain()
        raise
    app.store.event({"event": "stop"})
    app.notify(f"종료 — 누적 실현 {money(app.trader.state.cum_net)}, 열린 포지션 "
               f"{[s for s, p in app.trader.state.positions.items() if p] or '없음'}")
    _obsidian(f"pastofi {args.mode} 종료 — 누적 {money(app.trader.state.cum_net)}")
    app.notify.drain()


def _obsidian(text: str) -> None:
    try:
        from ...obsidian import append_daily_log
        append_daily_log(text, heading="pastofi 코인판")
    except Exception as e:
        log.warning("옵시디언 로그 실패: %s", e)


if __name__ == "__main__":
    main()
