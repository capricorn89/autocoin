"""체결 누락 자동 백필 (수집기 안에서 실행).

 - 수집 재개 후: 수집기가 결측 구간(depth 무수신 ≥ outage_record_s)을 기록하면 settle_s(30초) 뒤
   [결측 시작 - 5분, 지금] 구간의 aggTrade id 갭을 찾아 백필한다.
 - 정기 점검: 시작 startup_delay_s(1분) 뒤 1회, 이후 sweep_interval_s(6시간)마다 최근 sweep_hours(72시간).
   재연결 틈의 소량 누락(3분 기준에 안 걸림)과, REST 2일 제한 때문에 다음 날 게시되는 아카이브로만
   채울 수 있는 구간을 재시도한다.
 - 복구 경로는 integrity.backfill_agg_gaps 와 같다: REST(최근 2일) → 한도 초과·2일 경과 시 아카이브.
 - 채운 게 있거나 못 채운 게 남았을 때만 알린다. 실패해도 수집은 계속한다.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Callable

import psycopg

from .exchange.rest import FuturesRestClient
from .integrity import backfill_agg_gaps, find_agg_gaps

log = logging.getLogger(__name__)


class AutoBackfiller:
    def __init__(self, dsn: str, symbol: str, notify: Callable[[str], None] | None = None,
                 rest_factory: Callable[[], object] = FuturesRestClient,
                 settle_s: float = 30.0, startup_delay_s: float = 60.0,
                 sweep_interval_s: float = 6 * 3600, sweep_hours: float = 72.0,
                 lead_s: float = 300.0, clock: Callable[[], float] = time.time,
                 poll_s: float = 15.0):
        self.dsn = dsn
        self.symbol = symbol
        self._notify = notify
        self._rest_factory = rest_factory
        self.settle_s = settle_s
        self.sweep_interval_s = sweep_interval_s
        self.sweep_hours = sweep_hours
        self.lead_s = lead_s
        self._clock = clock
        self.poll_s = poll_s
        self._pending: list[tuple[float, float]] = []      # (결측 시작, 실행 예정 시각)
        self._next_sweep = clock() + startup_delay_s

    def on_outage(self, start: float, end: float) -> None:
        """수집기 결측 기록 콜백 (이벤트 루프에서 호출)."""
        self._pending.append((start, end + self.settle_s))

    def due_jobs(self, now: float) -> list[tuple[float, float, str]]:
        """지금 실행할 (구간 시작, 구간 끝, 사유) 목록. 호출하면 해당 작업은 소비된다."""
        jobs, keep = [], []
        for start, due in self._pending:
            (jobs if now >= due else keep).append((start, due))
        self._pending = keep
        out = [(start - self.lead_s, now, "수집 재개") for start, _ in jobs]
        if now >= self._next_sweep:
            out.append((now - self.sweep_hours * 3600, now, "정기 점검"))
            self._next_sweep = now + self.sweep_interval_s
        return out

    def run_once(self, start: float, end: float) -> dict:
        """구간 [start, end] 의 체결 id 갭을 찾아 백필 (블로킹, 별도 스레드에서 실행)."""
        s = datetime.fromtimestamp(start, tz=timezone.utc)
        e = datetime.fromtimestamp(end, tz=timezone.utc) + timedelta(seconds=1)
        with psycopg.connect(self.dsn) as conn:
            gaps = find_agg_gaps(conn, self.symbol, s, e)
            missing = sum(g[1] - g[0] + 1 for g in gaps)
            filled = backfill_agg_gaps(conn, self._rest_factory(), self.symbol, gaps) if gaps else 0
            left = find_agg_gaps(conn, self.symbol, s, e) if gaps else []
        return {"gaps": len(gaps), "missing": missing, "filled": filled,
                "remaining": sum(g[1] - g[0] + 1 for g in left)}

    def _report(self, reason: str, res: dict) -> None:
        if not res["missing"]:
            log.info("자동 백필(%s): %s 누락 없음", reason, self.symbol)
            return
        log.info("자동 백필(%s): %s 누락 %d건 중 %d건 복구, 남음 %d건", reason, self.symbol,
                 res["missing"], res["filled"], res["remaining"])
        if self._notify is None:
            return
        msg = (f"🧩 {self.symbol} 체결 자동 백필({reason}) — 누락 {res['missing']:,}건 중 "
               f"{res['filled']:,}건 복구")
        if res["remaining"]:
            msg += (f", {res['remaining']:,}건 남음. 아카이브가 게시되면(다음 날) 정기 점검에서 "
                    "다시 시도해요.")
        self._notify(msg)

    async def run(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            for start, end, reason in self.due_jobs(self._clock()):
                try:
                    res = await asyncio.to_thread(self.run_once, start, end)
                except Exception as e:   # 백필 실패가 수집을 멈추면 안 된다 — 기록만 하고 다음 주기에 재시도
                    log.exception("자동 백필(%s) 실패: %s", reason, type(e).__name__)
                    if self._notify is not None:
                        self._notify(f"❗ {self.symbol} 체결 자동 백필({reason}) 실패 — "
                                     f"{type(e).__name__}. 다음 정기 점검에서 다시 시도해요.")
                    continue
                self._report(reason, res)
            try:
                await asyncio.wait_for(stop.wait(), timeout=self.poll_s)
            except TimeoutError:
                pass
