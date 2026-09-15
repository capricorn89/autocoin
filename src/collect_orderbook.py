"""Binance USDT-M 오더북/체결 WebSocket 수집 스크립트.

예)
  python -m src.collect_orderbook                          # BTCUSDT, EWYUSDT 무기한
  python -m src.collect_orderbook --symbols BTCUSDT --duration 300
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import signal

from .exchange.sinks import JsonlSink
from .exchange.ws import CollectorConfig, OrderBookCollector
from .logging_setup import setup_logging

log = logging.getLogger("collector")


async def _amain(args: argparse.Namespace) -> dict:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    if args.duration > 0:
        loop.call_later(args.duration, stop.set)

    cfg = CollectorConfig(symbols=[s.upper() for s in args.symbols],
                          depth_speed=args.depth_speed,
                          stale_timeout=args.stale_timeout,
                          status_interval=args.status_interval)
    sink = JsonlSink(args.out)
    collector = OrderBookCollector(cfg, sink)
    log.info("수집 시작: %s → %s (duration=%s)", cfg.symbols, args.out,
             f"{args.duration}s" if args.duration > 0 else "무기한")
    try:
        await collector.run(stop)
    finally:
        sink.close()
    summary = collector.summary()
    log.info("수집 종료 요약: %s", json.dumps(summary, ensure_ascii=False))
    return summary


def main() -> None:
    ap = argparse.ArgumentParser(description="Binance USDT-M 오더북/체결 WS 수집")
    ap.add_argument("--symbols", nargs="+", default=["BTCUSDT", "EWYUSDT"])
    ap.add_argument("--duration", type=float, default=0, help="초. 0 = 무기한")
    ap.add_argument("--out", default="data/raw")
    ap.add_argument("--depth-speed", default="100ms", choices=["100ms", "250ms", "500ms"])
    ap.add_argument("--stale-timeout", type=float, default=10.0)
    ap.add_argument("--status-interval", type=float, default=60.0)
    ap.add_argument("--log-level", default="INFO")
    ap.add_argument("--log-file", default="logs/collector.log")
    args = ap.parse_args()
    setup_logging(args.log_level, args.log_file)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
