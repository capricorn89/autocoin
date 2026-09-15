"""Binance USDT-M Futures WebSocket 오더북/체결 수집기.

구독 (연결 2개, 서로 독립적으로 재연결):
 - depth : wss://fstream.binance.com/public/stream?streams=<symbol>@depth@<speed>/...
 - trade : wss://fstream.binance.com/market/stream?streams=<symbol>@aggTrade/...
 Binance 가 2026-03-05 경로를 /public·/market·/private 로 분리했고, 레거시 /ws·/stream 은
 폐기 공지(2026-04-23) 상태다. 실측상 레거시 /stream 은 depth 만 오고 aggTrade 는 오지 않는다.

안정성 처리
 - 재연결: 네트워크 오류/서버 종료 시 지수 백오프(+지터)로 재연결. 연결 성공 시 백오프 초기화.
 - heartbeat: 클라이언트가 ping_interval 마다 ping, ping_timeout 내 pong 없으면 끊고 재연결.
   (Binance 서버 ping 에 대한 pong 은 websockets 라이브러리가 자동 응답)
 - stale 감지: 연결은 살아있는데 stale_timeout 동안 메시지가 없으면 재연결.
 - 24h 만료: Binance 는 연결을 24시간 후 끊으므로 max_session_seconds 에 선제 재연결.
 - 시퀀스 갭: depth pu 불연속 → gap 레코드 남기고 REST 스냅샷으로 재동기화.
             aggTrade id 불연속 → gap 레코드(from_id~to_id)만 남김. 복구(REST 백필)는 M2.
 - 재연결 시 오더북은 전부 무효화하고 다시 동기화한다.

가정 (Binance 문서 기준, 2026-09-15 확인한 동작은 decision 노트 참고)
 - depth diff 이벤트는 U/u/pu 필드를 가지며 pu == 직전 u 로 연속된다 (실측 확인).
 - 연결 최대 24시간, 연결당 수신 메시지 제한은 수집 용도에서는 문제되지 않는 수준.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import Counter
from dataclasses import dataclass, field
from typing import Awaitable, Callable

import websockets
from websockets.exceptions import ConnectionClosed, InvalidHandshake

from .orderbook import LocalOrderBook, SequenceGapError
from .rest import FuturesRestClient
from .sinks import Sink

log = logging.getLogger(__name__)

FSTREAM_BASE = "wss://fstream.binance.com"

SnapshotFetcher = Callable[[str], Awaitable[dict]]


class StaleStreamError(ConnectionError):
    """stale_timeout 동안 메시지 없음 — 연결은 열려 있어도 데이터가 끊긴 상태."""


class SessionExpired(ConnectionError):
    """24h 연결 만료 전 선제 재연결."""


@dataclass
class CollectorConfig:
    symbols: list[str]
    ws_base: str = FSTREAM_BASE
    # 2026-03 Binance 경로 분리: 호가(depth)는 /public, 체결(aggTrade)은 /market 에서만 수신됨.
    # 2026-09-15 실측: /market 에서 depth 0건, /public 과 레거시 /stream 에서 aggTrade 0건.
    depth_route: str = "/public"
    trade_route: str = "/market"
    depth_speed: str = "100ms"        # 100ms / 250ms / 500ms
    snapshot_limit: int = 1000
    top_levels: int = 5                # 상위 N호가가 바뀔 때마다 book_top 레코드 발행 (0 = 끔)
    # EWYUSDT 는 24/7 거래되지만 주말 새벽(KST) 무체결 분이 최대 16% (1m봉 2026-03~08 분석).
    stale_timeout: float = 30.0         # depth 연결
    trade_stale_timeout: float = 300.0  # 체결 연결 — 저유동 구간 오탐 재연결 방지
    ping_interval: float = 20.0
    ping_timeout: float = 20.0
    max_session_seconds: float = 23 * 3600
    backoff_base: float = 1.0
    backoff_max: float = 60.0
    status_interval: float = 60.0
    extra: dict = field(default_factory=dict)

    def stream_urls(self) -> dict[str, str]:
        syms = [s.lower() for s in self.symbols]
        depth = "/".join(f"{s}@depth@{self.depth_speed}" for s in syms)
        trade = "/".join(f"{s}@aggTrade" for s in syms)
        return {"depth": f"{self.ws_base}{self.depth_route}/stream?streams={depth}",
                "trade": f"{self.ws_base}{self.trade_route}/stream?streams={trade}"}


class BookSynchronizer:
    """심볼 1개의 diff 버퍼링 → 스냅샷 → 재생 → 실시간 적용 상태 머신."""

    def __init__(self, symbol: str, fetch_snapshot: SnapshotFetcher,
                 emit: Callable[[dict], None], clock: Callable[[], float] = time.time,
                 retry_delay: float = 1.0, max_retry_delay: float = 30.0,
                 max_buffer: int = 10_000):
        self.book = LocalOrderBook(symbol)
        self._fetch = fetch_snapshot
        self._emit = emit
        self._clock = clock
        self.retry_delay = retry_delay
        self.max_retry_delay = max_retry_delay
        self.max_buffer = max_buffer
        self._buffer: list[dict] = []
        self._task: asyncio.Task | None = None
        self.gaps = 0
        self.resyncs = 0

    @property
    def symbol(self) -> str:
        return self.book.symbol

    @property
    def synced(self) -> bool:
        return self._task is None and self.book.last_update_id is not None

    def on_diff(self, event: dict) -> None:
        if not self.synced:
            self._buffer.append(event)
            if len(self._buffer) > self.max_buffer:
                # 스냅샷이 장시간 실패하는 경우 메모리 보호. 앞부분이 잘리면 재생 시 갭으로 재시도됨.
                del self._buffer[: len(self._buffer) - self.max_buffer]
            self._ensure_resync()
            return
        try:
            self.book.apply_diff(event)
        except SequenceGapError as e:
            self._on_gap(str(e), event)

    def reset(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._buffer.clear()
        self.book.reset()

    def _on_gap(self, detail: str, event: dict) -> None:
        self.gaps += 1
        log.warning("depth 시퀀스 갭 — 재동기화: %s", detail)
        self._emit({"kind": "gap", "stream": "depth", "symbol": self.symbol,
                    "recv_ts": int(self._clock() * 1000), "detail": detail,
                    "U": event.get("U"), "u": event.get("u"), "pu": event.get("pu")})
        self.book.reset()
        self._buffer = [event]
        self._ensure_resync()

    def _ensure_resync(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.get_running_loop().create_task(self._resync())

    async def _resync(self) -> None:
        delay = self.retry_delay
        try:
            while True:
                try:
                    snap = await self._fetch(self.symbol)
                except Exception as e:  # REST 장애는 수집을 멈추지 않고 재시도 (diff 는 계속 버퍼링)
                    log.warning("%s 스냅샷 실패 — %.1fs 후 재시도: %s", self.symbol, delay, e)
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, self.max_retry_delay)
                    continue

                self.resyncs += 1
                self.book.apply_snapshot(snap)
                self._emit({"kind": "snapshot", "symbol": self.symbol,
                            "recv_ts": int(self._clock() * 1000),
                            "lastUpdateId": snap["lastUpdateId"], "E": snap.get("E"),
                            "bids": snap["bids"], "asks": snap["asks"]})

                pending, self._buffer = self._buffer, []
                for i, ev in enumerate(pending):
                    try:
                        self.book.apply_diff(ev)
                    except SequenceGapError as e:
                        log.info("%s 스냅샷-버퍼 정합 실패, 스냅샷 재요청: %s", self.symbol, e)
                        self.book.reset()
                        self._buffer = pending[i:] + self._buffer
                        break
                else:
                    log.info("%s 오더북 동기화 완료 (lastUpdateId=%s, 재생 %d건)",
                             self.symbol, self.book.last_update_id, len(pending))
                    return
                await asyncio.sleep(delay)
                delay = min(delay * 2, self.max_retry_delay)
        finally:
            if self._task is asyncio.current_task():
                self._task = None


class OrderBookCollector:
    def __init__(self, cfg: CollectorConfig, sink: Sink,
                 fetch_snapshot: SnapshotFetcher | None = None,
                 rest: FuturesRestClient | None = None,
                 connect=websockets.connect,
                 clock: Callable[[], float] = time.time):
        self.cfg = cfg
        self.sink = sink
        self._connect = connect
        self._clock = clock
        if fetch_snapshot is None:
            client = rest or FuturesRestClient()

            async def fetch_snapshot(symbol: str) -> dict:
                return await asyncio.to_thread(client.depth_snapshot, symbol, cfg.snapshot_limit)

        self.books = {s.upper(): BookSynchronizer(s.upper(), fetch_snapshot, self.sink.write, clock)
                      for s in cfg.symbols}
        self._last_agg_id: dict[str, int] = {}
        self._last_top: dict[str, tuple] = {}
        self.stats: Counter = Counter()

    # ---- 메인 루프 ----
    async def run(self, stop: asyncio.Event) -> None:
        """depth(/public) 와 trade(/market) 연결을 독립적으로 유지. 한쪽 장애가 다른 쪽을 끊지 않는다."""
        urls = self.cfg.stream_urls()
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._connection_loop("depth", urls["depth"],
                                                 self.cfg.stale_timeout, stop))
            tg.create_task(self._connection_loop("trade", urls["trade"],
                                                 self.cfg.trade_stale_timeout, stop))
        self.sink.flush()

    async def _connection_loop(self, name: str, url: str, stale_timeout: float,
                               stop: asyncio.Event) -> None:
        attempt = 0
        while not stop.is_set():
            reason = ""
            planned = False
            try:
                async with self._connect(url, ping_interval=self.cfg.ping_interval,
                                         ping_timeout=self.cfg.ping_timeout,
                                         open_timeout=10, close_timeout=5) as ws:
                    self.stats[f"{name}.connects"] += 1
                    attempt = 0
                    self._conn_event(name, "connected")
                    log.info("WS[%s] 연결: %s", name, url)
                    await self._session(name, ws, stale_timeout, stop)
            except (OSError, ConnectionClosed, InvalidHandshake) as e:
                # StaleStreamError / SessionExpired / TimeoutError 는 OSError 하위
                reason = f"{type(e).__name__}: {e}"
                planned = isinstance(e, SessionExpired)
            finally:
                if name == "depth":   # 끊긴 동안의 diff 는 복구 불가 → 오더북 무효화 후 재동기화
                    for b in self.books.values():
                        b.reset()
                    self._last_top.clear()

            if stop.is_set():
                break
            self.stats[f"{name}.reconnects"] += 1
            self._conn_event(name, "disconnected", reason)
            delay = 0.0 if planned else (
                min(self.cfg.backoff_max, self.cfg.backoff_base * 2 ** attempt)
                * random.uniform(0.5, 1.0))
            attempt += 1
            log.warning("WS[%s] 끊김(%s) — %.1fs 후 재연결", name, reason, delay)
            try:
                await asyncio.wait_for(stop.wait(), timeout=delay)
            except TimeoutError:
                pass

        self._conn_event(name, "stopped")

    async def _session(self, name: str, ws, stale_timeout: float, stop: asyncio.Event) -> None:
        started = last_msg = last_status = time.monotonic()
        poll = min(1.0, stale_timeout)
        while not stop.is_set():
            if time.monotonic() - started >= self.cfg.max_session_seconds:
                raise SessionExpired(f"{self.cfg.max_session_seconds:.0f}s 경과 — 선제 재연결")
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=poll)
            except TimeoutError:
                if time.monotonic() - last_msg >= stale_timeout:
                    raise StaleStreamError(f"{stale_timeout:.0f}s 동안 메시지 없음")
                continue
            last_msg = time.monotonic()
            self.handle_message(raw)
            if name == "depth" and last_msg - last_status >= self.cfg.status_interval:
                last_status = last_msg
                self._log_status()
                self.sink.flush()

    # ---- 메시지 처리 ----
    def handle_message(self, raw: str | bytes) -> None:
        msg = json.loads(raw)
        data = msg.get("data", msg)
        etype, sym = data.get("e"), data.get("s")
        recv_ts = int(self._clock() * 1000)
        if etype == "depthUpdate" and sym in self.books:
            self.stats[f"{sym}.depth"] += 1
            self.sink.write({"kind": "depth", "symbol": sym, "recv_ts": recv_ts, **data})
            sync = self.books[sym]
            sync.on_diff(data)
            if self.cfg.top_levels and sync.synced and sync.book.ready:
                self._emit_top(sym, sync.book, data, recv_ts)
        elif etype == "aggTrade" and sym in self.books:
            self.stats[f"{sym}.trade"] += 1
            self.sink.write({"kind": "trade", "symbol": sym, "recv_ts": recv_ts, **data})
            self._check_trade_seq(sym, data, recv_ts)
        else:
            log.debug("미처리 메시지: %s", str(msg)[:200])

    def _emit_top(self, sym: str, book: LocalOrderBook, event: dict, recv_ts: int) -> None:
        """상위 N호가(가격·수량)가 직전 발행과 다를 때만 book_top 레코드 발행."""
        bids, asks = book.top(self.cfg.top_levels)
        key = (tuple(bids), tuple(asks))
        if self._last_top.get(sym) == key:
            return
        self._last_top[sym] = key
        self.stats[f"{sym}.book_top"] += 1
        self.sink.write({"kind": "book_top", "symbol": sym, "recv_ts": recv_ts,
                         "E": event.get("E"), "T": event.get("T"), "u": book.last_update_id,
                         "bids": [list(x) for x in bids], "asks": [list(x) for x in asks]})

    def _check_trade_seq(self, sym: str, data: dict, recv_ts: int) -> None:
        a = data.get("a")
        if a is None:
            return
        a = int(a)
        prev = self._last_agg_id.get(sym)
        if prev is not None and a != prev + 1:
            self.stats[f"{sym}.trade_gap"] += 1
            detail = "역전/중복" if a <= prev else "누락"
            log.warning("%s aggTrade id %s: 직전 %d → %d", sym, detail, prev, a)
            self.sink.write({"kind": "gap", "stream": "aggTrade", "symbol": sym,
                             "recv_ts": recv_ts, "detail": detail,
                             "from_id": prev + 1, "to_id": a - 1})
        if prev is None or a > prev:
            self._last_agg_id[sym] = a

    def _conn_event(self, stream: str, event: str, reason: str = "") -> None:
        self.sink.write({"kind": "conn", "stream": stream, "event": event, "reason": reason,
                         "recv_ts": int(self._clock() * 1000)})

    def _log_status(self) -> None:
        for sym, b in self.books.items():
            bb, ba = b.book.best_bid(), b.book.best_ask()
            log.info("[%s] synced=%s bid=%s ask=%s depth=%d trade=%d gaps=%d resyncs=%d",
                     sym, b.synced, bb and bb[0], ba and ba[0], self.stats[f"{sym}.depth"],
                     self.stats[f"{sym}.trade"], b.gaps, b.resyncs)
            if b.synced and b.book.is_crossed():
                log.error("[%s] 오더북 교차(bid>=ask) — 데이터 손상 의심", sym)

    def summary(self) -> dict:
        out = dict(self.stats)
        for sym, b in self.books.items():
            out[f"{sym}.depth_gaps"] = b.gaps
            out[f"{sym}.resyncs"] = b.resyncs
        return out
