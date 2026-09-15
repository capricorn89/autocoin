"""수집기 레코드 → TimescaleDB 배치 적재 싱크.

적재 대상 kind
 - trade     → market.trades
 - book_top  → market.book_top5
 - gap / conn / snapshot(메타만) / clock / sink_error → market.collector_events
 - depth(원본 diff) 는 저장하지 않는다 (5호가 변경분만 저장하기로 결정).

장애 처리
 - DB 오류가 나도 write() 는 예외를 던지지 않는다 (수집을 멈추지 않음). 버퍼에 쌓고 지수 백오프로 재시도.
 - 버퍼가 max_buffer_rows 를 넘거나 종료 시점에 적재가 안 되면 spill JSONL 로 떨어뜨리고 on_spill 콜백 호출.
   → python -m src.storage.db replay-spill <파일> 로 재적재.
 - PK 충돌(중복 수신, 재적재)은 ON CONFLICT DO NOTHING.
"""
from __future__ import annotations

import json
import logging
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import psycopg

from .db import get_dsn

log = logging.getLogger(__name__)

TOP_LEVELS = 5
TRADE_COLS = ("exchange_ts", "symbol", "agg_id", "price", "qty", "first_trade_id",
              "last_trade_id", "is_buyer_maker", "event_ts", "recv_ts", "source")
TOP_COLS = ("exchange_ts", "symbol", "update_id", "event_ts", "recv_ts") + tuple(
    f"{side}_{field}_{i}" for side in ("bid", "ask") for field in ("px", "qty")
    for i in range(1, TOP_LEVELS + 1))
EVENT_COLS = ("recv_ts", "kind", "symbol", "stream", "detail")
EVENT_KINDS = {"gap", "conn", "snapshot", "clock", "sink_error"}


def _insert_sql(table: str, cols: tuple[str, ...]) -> str:
    return (f"INSERT INTO {table} ({', '.join(cols)}) "
            f"VALUES ({', '.join(['%s'] * len(cols))}) ON CONFLICT DO NOTHING")


TRADE_SQL = _insert_sql("market.trades", TRADE_COLS)
TOP_SQL = _insert_sql("market.book_top5", TOP_COLS)
EVENT_SQL = _insert_sql("market.collector_events", EVENT_COLS)


def _ts(ms) -> datetime | None:
    return None if ms is None else datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc)


def trade_row(r: dict, symbol: str | None = None, source: str = "ws") -> tuple:
    return (_ts(r["T"]), symbol or r.get("symbol") or r["s"], int(r["a"]), float(r["p"]),
            float(r["q"]), int(r["f"]), int(r["l"]), bool(r["m"]), _ts(r.get("E")),
            _ts(r.get("recv_ts")), source)


def _levels(levels) -> tuple[list, list]:
    px = [float(p) for p, _ in levels[:TOP_LEVELS]]
    qty = [float(q) for _, q in levels[:TOP_LEVELS]]
    pad = [None] * (TOP_LEVELS - len(px))
    return px + pad, qty + pad


def top_row(r: dict) -> tuple:
    bp, bq = _levels(r["bids"])
    ap, aq = _levels(r["asks"])
    exch = r.get("T") or r["E"]
    return (_ts(exch), r["symbol"], int(r["u"]), _ts(r["E"]), _ts(r["recv_ts"]),
            *bp, *bq, *ap, *aq)


def event_row(r: dict) -> tuple:
    detail = {k: v for k, v in r.items()
              if k not in ("recv_ts", "kind", "symbol", "stream", "bids", "asks")}
    if r.get("kind") == "snapshot":
        detail["bid_levels"] = len(r.get("bids", []))
        detail["ask_levels"] = len(r.get("asks", []))
    return (_ts(r["recv_ts"]), r["kind"], r.get("symbol"), r.get("stream"),
            json.dumps(detail, ensure_ascii=False))


_ROUTES = {"trade": (TRADE_SQL, trade_row), "book_top": (TOP_SQL, top_row),
           "event": (EVENT_SQL, event_row)}


class PostgresSink:
    def __init__(self, dsn: str | None = None, flush_rows: int = 500, flush_seconds: float = 1.0,
                 max_buffer_rows: int = 200_000, spill_dir: str | Path = "data/spill",
                 on_spill: Callable[[Path, int, str], None] | None = None,
                 connect: Callable = psycopg.connect,
                 clock: Callable[[], float] = time.monotonic):
        self.dsn = get_dsn(dsn)
        self.flush_rows = flush_rows
        self.flush_seconds = flush_seconds
        self.max_buffer_rows = max_buffer_rows
        self.spill_dir = Path(spill_dir)
        self.on_spill = on_spill
        self._connect = connect
        self._clock = clock
        self._conn: psycopg.Connection | None = None
        self._buf: dict[str, list[dict]] = {"trade": [], "book_top": [], "event": []}
        self._last_flush = clock()
        self._retry_at = 0.0
        self._consecutive_failures = 0
        self.inserted: Counter = Counter()
        self.failures = 0
        self.spills = 0
        self.dropped = 0

    # ---- Sink 인터페이스 ----
    def check_connection(self) -> None:
        """시작 시 DB·스키마가 준비됐는지 확인 (없으면 예외 — 조용히 유실하지 않음)."""
        conn = self._get_conn()
        conn.execute("SELECT 1 FROM market.trades LIMIT 0")
        conn.execute("SELECT 1 FROM market.book_top5 LIMIT 0")
        conn.rollback()

    def write(self, record: dict) -> None:
        kind = record.get("kind")
        if kind in ("trade", "book_top"):
            self._buf[kind].append(record)
        elif kind in EVENT_KINDS:
            self._buf["event"].append(record)
        else:
            return
        if self.pending > self.max_buffer_rows:
            self._spill("버퍼 한도 초과 (DB 적재 지연)")
            return
        now = self._clock()
        if now >= self._retry_at and (self.pending >= self.flush_rows
                                      or now - self._last_flush >= self.flush_seconds):
            self.flush()

    @property
    def pending(self) -> int:
        return sum(len(v) for v in self._buf.values())

    def flush(self) -> bool:
        self._last_flush = self._clock()
        if not self.pending:
            return True
        batches: dict[str, list[tuple]] = {}
        for key, (_, conv) in _ROUTES.items():
            rows = []
            for r in self._buf[key]:
                try:
                    rows.append(conv(r))
                except (KeyError, TypeError, ValueError) as e:
                    self.dropped += 1
                    log.error("잘못된 %s 레코드 폐기(%s): %s", key, e, str(r)[:200])
            batches[key] = rows
        try:
            conn = self._get_conn()
            with conn.cursor() as cur:
                for key, (stmt, _) in _ROUTES.items():
                    if batches[key]:
                        cur.executemany(stmt, batches[key])
            conn.commit()
        except psycopg.Error as e:
            self.failures += 1
            self._consecutive_failures += 1
            wait = min(30.0, 2.0 ** self._consecutive_failures)
            self._retry_at = self._clock() + wait
            log.error("DB 적재 실패(%d회 연속, 대기 %d행, %.0fs 후 재시도): %s",
                      self._consecutive_failures, self.pending, wait,
                      str(e).strip().splitlines()[0] if str(e).strip() else type(e).__name__)
            self._close_conn()
            return False
        for key in _ROUTES:
            self.inserted[key] += len(batches[key])
            self._buf[key].clear()
        self._consecutive_failures = 0
        self._retry_at = 0.0
        return True

    def close(self) -> None:
        if self.pending and not self.flush():
            self._spill("종료 시점 DB 적재 실패")
        self._close_conn()

    # ---- 내부 ----
    def _get_conn(self) -> psycopg.Connection:
        if self._conn is None or self._conn.closed:
            self._conn = self._connect(self.dsn, autocommit=False)
        return self._conn

    def _close_conn(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except psycopg.Error:
                pass
        self._conn = None

    def _spill(self, reason: str) -> Path:
        self.spill_dir.mkdir(parents=True, exist_ok=True)
        records = [r for key in _ROUTES for r in self._buf[key]]
        path = self.spill_dir / f"{datetime.now(timezone.utc):%Y%m%dT%H%M%S%f}_{len(records)}.jsonl"
        with path.open("w", encoding="utf-8") as fh:
            for r in records:
                fh.write(json.dumps(r, ensure_ascii=False, separators=(",", ":")) + "\n")
        for key in _ROUTES:
            self._buf[key].clear()
        self.spills += 1
        log.error("DB 미적재 %d행을 spill 파일로 저장: %s (%s)", len(records), path, reason)
        if self.on_spill is not None:
            self.on_spill(path, len(records), reason)
        return path


def replay_spill(path: str | Path, dsn: str | None = None) -> int:
    sink = PostgresSink(dsn, flush_rows=10 ** 9, flush_seconds=10 ** 9)
    n = 0
    with Path(path).open(encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                sink.write(json.loads(line))
                n += 1
    if not sink.flush():
        raise RuntimeError(f"재적재 실패: {path}")
    sink._close_conn()
    return n
