"""Binance USDT-M 체결 + 매수/매도 5호가 수집 → TimescaleDB 누적.

예)
  python -m src.collect_orderbook                          # EWYUSDT 무기한, DB 적재
  python -m src.collect_orderbook --duration 300 --raw-dir data/raw   # 원본 JSONL 도 함께 저장

시작 시 확인 (하나라도 실패하면 즉시 종료 — 조용히 유실하지 않음)
 - OBSIDIAN_AUTOJI_PATH/crypto_testbed 존재 (--no-obsidian 으로 끌 수 있음)
 - DB 접속 + 스키마 존재 (python -m src.storage.db init 먼저 실행)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

from . import obsidian
from .exchange.rest import FuturesRestClient, measure_clock_offset
from .exchange.sinks import FanoutSink, JsonlSink
from .exchange.ws import CollectorConfig, OrderBookCollector
from .logging_setup import setup_logging
from .storage.pg_sink import PostgresSink

log = logging.getLogger("collector")


def _spill_handler(vault: Path | None):
    def on_spill(path: Path, rows: int, reason: str) -> None:
        if vault is None:
            return
        obsidian.write_incident(
            "DB 적재 실패 — spill 파일 발생",
            meta={"분류": "데이터 적재", "spill파일": str(path), "행수": rows},
            body=(f"## 증상\n\n{reason}. DB 에 적재하지 못한 {rows:,}행을 로컬 파일로 저장했다.\n\n"
                  f"- 파일: `{path}`\n\n## 복구\n\n```bash\n"
                  f"python -m src.storage.db replay-spill {path}\n```\n\n"
                  "## 원인\n\n(조사 후 기록)\n"),
            root=vault)
    return on_spill


async def _amain(args: argparse.Namespace) -> dict:
    vault = None if args.no_obsidian else obsidian.testbed_root()

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.duration > 0:
        loop.call_later(args.duration, stop.set)

    sinks = []
    pg = None
    if not args.no_db:
        pg = PostgresSink(args.dsn, on_spill=_spill_handler(vault))
        pg.check_connection()
        sinks.append(pg)
    if args.raw_dir:
        sinks.append(JsonlSink(args.raw_dir))
    if not sinks:
        raise SystemExit("저장소가 없습니다: --no-db 를 쓰려면 --raw-dir 을 지정하세요.")
    sink = FanoutSink(sinks)

    rest = FuturesRestClient()
    clock = await asyncio.to_thread(measure_clock_offset, rest)
    sink.write({"kind": "clock", "recv_ts": int(time.time() * 1000), **clock})
    log.info("로컬-서버 시계 오프셋 %+.1fms (rtt %.1fms, +면 로컬이 빠름)",
             clock["offset_ms"], clock["rtt_ms"])

    cfg = CollectorConfig(symbols=[s.upper() for s in args.symbols],
                          depth_speed=args.depth_speed, top_levels=args.top_levels,
                          stale_timeout=args.stale_timeout,
                          trade_stale_timeout=args.trade_stale_timeout,
                          status_interval=args.status_interval)
    collector = OrderBookCollector(cfg, sink, rest=rest)
    started = datetime.now(timezone.utc)
    log.info("수집 시작: %s (duration=%s, db=%s, raw=%s)", cfg.symbols,
             f"{args.duration}s" if args.duration > 0 else "무기한",
             "off" if pg is None else pg.dsn, args.raw_dir or "off")
    try:
        await collector.run(stop)
    finally:
        sink.close()
    ended = datetime.now(timezone.utc)

    summary = collector.summary()
    summary["clock_offset_ms"] = clock["offset_ms"]
    if pg is not None:
        summary.update({f"db.{k}": v for k, v in pg.inserted.items()})
        summary.update({"db.failures": pg.failures, "db.spills": pg.spills, "db.dropped": pg.dropped})
    log.info("수집 종료 요약: %s", json.dumps(summary, ensure_ascii=False))

    if vault is not None:
        s = summary
        lines = [f"- 구간: {started:%Y-%m-%d %H:%M:%S}Z ~ {ended:%Y-%m-%d %H:%M:%S}Z "
                 f"({str(ended - started).split('.')[0]})",
                 f"- 시계 오프셋: {clock['offset_ms']:+.1f}ms"]
        for sym in cfg.symbols:
            lines.append(f"- {sym}: 체결 {s.get(f'{sym}.trade', 0):,} · 5호가 변경 {s.get(f'{sym}.book_top', 0):,} · "
                         f"depth 갭 {s.get(f'{sym}.depth_gaps', 0)} · 체결 id 갭 {s.get(f'{sym}.trade_gap', 0)} · "
                         f"재동기화 {s.get(f'{sym}.resyncs', 0)}")
        lines.append(f"- 재연결: depth {s.get('depth.reconnects', 0)} / trade {s.get('trade.reconnects', 0)}")
        if pg is not None:
            lines.append(f"- DB 적재: 체결 {s.get('db.trade', 0):,} · 5호가 {s.get('db.book_top', 0):,} · "
                         f"이벤트 {s.get('db.event', 0):,} · 실패 {pg.failures} · spill {pg.spills}")
        obsidian.append_daily_log("\n".join(lines), heading="수집기 실행 종료", root=vault)
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance USDT-M 체결·5호가 수집 → TimescaleDB")
    ap.add_argument("--symbols", nargs="+", default=["EWYUSDT"])
    ap.add_argument("--duration", type=float, default=0, help="초. 0 = 무기한")
    ap.add_argument("--dsn", default=None, help="기본: AUTOCOIN_PG_DSN 또는 postgresql:///autocoin")
    ap.add_argument("--no-db", action="store_true")
    ap.add_argument("--raw-dir", default=None, help="원본 이벤트 JSONL 저장 경로 (기본 끔)")
    ap.add_argument("--no-obsidian", action="store_true", help="Obsidian 일일 로그/사고 기록 끔")
    ap.add_argument("--top-levels", type=int, default=5)
    ap.add_argument("--depth-speed", default="100ms", choices=["100ms", "250ms", "500ms"])
    ap.add_argument("--stale-timeout", type=float, default=30.0)
    ap.add_argument("--trade-stale-timeout", type=float, default=300.0)
    ap.add_argument("--status-interval", type=float, default=60.0)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="logs/collector.log")
    args = ap.parse_args()
    setup_logging(args.log_level, args.log_file)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
