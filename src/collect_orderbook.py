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
from .autobackfill import AutoBackfiller
from .exchange.rest import FuturesRestClient, measure_clock_offset
from .exchange.sinks import FanoutSink, JsonlSink
from .exchange.ws import CollectorConfig, OrderBookCollector
from .logging_setup import setup_logging
from .monitor import CollectionMonitor
from .notify import TelegramNotifier
from .power import SleepGuard, SleepPolicy
from .storage.db import last_book_ts
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
                          status_interval=args.status_interval,
                          outage_record_s=args.alert_after)
    collector = OrderBookCollector(cfg, sink, rest=rest)

    # 수집 끊김 알림: 텔레그램 설정이 없으면 경고만 남기고 수집은 계속한다.
    monitor = None
    label = "/".join(cfg.symbols)
    if not args.no_notify:
        notifier = TelegramNotifier.from_env()
        if notifier is None:
            log.warning("텔레그램 미설정 — ~/.config/autocoin/.env 에 TELEGRAM_BOT_TOKEN, "
                        "TELEGRAM_CHAT_ID 를 넣으면 수집 끊김 알림을 받습니다.")
        else:
            monitor = CollectionMonitor(notifier.send, collector, label, alert_after=args.alert_after)
            if pg is not None:   # 재부팅·중단으로 생긴 공백도 첫 수신 때 복구 알림으로 잡는다
                collector.last_depth_at = last_book_ts(pg.dsn, cfg.symbols[0])

    # 체결 누락 자동 백필: 수집 재개 후 + 정기 점검 (DB 적재할 때만)
    backfillers = []
    if pg is not None and not args.no_backfill:
        notify = monitor.queue.append if monitor is not None else None
        for sym in cfg.symbols:
            bf = AutoBackfiller(pg.dsn, sym, notify=notify, sweep_hours=args.sweep_hours,
                                sweep_interval_s=args.sweep_interval)
            collector.outage_listeners.append(bf.on_outage)
            backfillers.append(bf)

    def on_power(rec: dict) -> None:
        sink.write(rec)
        if monitor is not None:
            monitor.on_power(rec)

    guard = (SleepGuard(SleepPolicy(args.battery_floor, args.net_grace), emit=on_power)
             if args.prevent_sleep else None)
    started = datetime.now(timezone.utc)
    log.info("수집 시작: %s (duration=%s, db=%s, raw=%s, 잠자기 방지=%s, 알림=%s)", cfg.symbols,
             f"{args.duration}s" if args.duration > 0 else "무기한",
             "off" if pg is None else pg.dsn, args.raw_dir or "off",
             f"AC 또는 배터리 {args.battery_floor}%+·네트워크" if guard else "off",
             f"텔레그램 {args.alert_after:.0f}s" if monitor else "off")
    try:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(collector.run(stop))
            if guard is not None:
                tg.create_task(guard.run(stop))
            if monitor is not None:
                tg.create_task(monitor.run(stop))
            for bf in backfillers:
                tg.create_task(bf.run(stop))
    finally:
        if guard is not None:
            guard.release()
        sink.close()
        if monitor is not None:
            await asyncio.to_thread(monitor.final, f"⏹ {label} 수집기 종료 — launchd 가 켜져 있으면 "
                                                   "30초 뒤 자동으로 다시 시작해요.")
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
    ap.add_argument("--console-level", default=None,
                    help="콘솔 로그 레벨 (launchd 실행 시 WARNING 권장 — stderr 파일은 회전되지 않음)")
    ap.add_argument("--prevent-sleep", action="store_true",
                    help="잠자기 방지: AC 전원이거나, 배터리 --battery-floor%% 이상 + 네트워크 연결일 때 "
                         "(src/power.py). 수집기 종료 시 자동 해제")
    ap.add_argument("--battery-floor", type=int, default=30,
                    help="배터리에서 잠자기를 막는 최소 잔량(%%)")
    ap.add_argument("--net-grace", type=float, default=300.0,
                    help="배터리에서 네트워크가 끊겨도 잠자기 방지를 유지하는 유예 시간(초)")
    ap.add_argument("--alert-after", type=float, default=180.0,
                    help="호가 수신이 이 시간(초) 넘게 없으면 텔레그램 끊김 알림")
    ap.add_argument("--no-notify", action="store_true", help="텔레그램 알림 끔 (수동 테스트용)")
    ap.add_argument("--no-backfill", action="store_true", help="체결 자동 백필 끔")
    ap.add_argument("--sweep-hours", type=float, default=72.0,
                    help="정기 점검에서 확인할 최근 시간(시간). REST 2일 + 아카이브 게시 지연 고려")
    ap.add_argument("--sweep-interval", type=float, default=6 * 3600,
                    help="정기 점검 주기(초)")
    args = ap.parse_args()
    setup_logging(args.log_level, args.log_file, console_level=args.console_level)
    asyncio.run(_amain(args))


if __name__ == "__main__":
    main()
