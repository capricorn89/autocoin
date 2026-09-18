"""적재 데이터 정합성 검사 (+ 체결 누락 REST 백필).

검사 항목
 - 결측 구간   : 체결 간 간격 > trade_gap 초, 5호가 행 간 간격 > book_gap 초
                (EWYUSDT 는 주말 새벽 무체결 분이 흔하므로 체결 결측은 '경고'로만 본다)
 - 시퀀스 갭   : aggTrade id(a) 불연속 구간 → --backfill 로 REST /fapi/v1/aggTrades 재수집
 - 시각 역전   : id 순서로 정렬했을 때 거래소 시각이 뒤로 가는 행 (체결: agg_id, 5호가: update_id)
 - 호가 이상   : 최우선 매수호가 >= 매도호가(교차), 최우선 호가 비어 있음
 - 수집기 이벤트: 기간 내 gap/conn/snapshot 건수, 마지막 시계 오프셋, 수신 지연 분위수

  python -m src.integrity                       # EWYUSDT 최근 24시간
  python -m src.integrity --hours 72 --backfill --obsidian
"""
from __future__ import annotations

import argparse
import logging
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import psycopg

from . import obsidian
from .exchange.rest import FuturesRestClient
from .logging_setup import setup_logging
from .storage.db import get_dsn
from .storage.pg_sink import TRADE_SQL, trade_row

log = logging.getLogger("integrity")
KST = ZoneInfo("Asia/Seoul")

_W_TRADES = "symbol = %(symbol)s AND exchange_ts >= %(start)s AND exchange_ts < %(end)s"


def run_checks(conn: psycopg.Connection, symbol: str, start: datetime, end: datetime,
               trade_gap_s: float = 60.0, book_gap_s: float = 10.0, limit: int = 10) -> dict:
    p = {"symbol": symbol, "start": start, "end": end, "tgap": trade_gap_s,
         "bgap": book_gap_s, "limit": limit}

    def rows(q: str) -> list[tuple]:
        return conn.execute(q, p).fetchall()

    def one(q: str):
        return conn.execute(q, p).fetchone()[0]

    r: dict = {"symbol": symbol, "start": start, "end": end,
               "trade_gap_s": trade_gap_s, "book_gap_s": book_gap_s}
    r["trades"] = rows(f"SELECT count(*), min(exchange_ts), max(exchange_ts) "
                       f"FROM market.trades WHERE {_W_TRADES}")[0]
    r["book_rows"] = rows(f"SELECT count(*), min(exchange_ts), max(exchange_ts) "
                          f"FROM market.book_top5 WHERE {_W_TRADES}")[0]

    r["agg_gaps"] = rows(f"""
        SELECT prev_id + 1 AS from_id, agg_id - 1 AS to_id, prev_ts, exchange_ts
        FROM (SELECT agg_id, exchange_ts, lag(agg_id) OVER w AS prev_id, lag(exchange_ts) OVER w AS prev_ts
              FROM market.trades WHERE {_W_TRADES} WINDOW w AS (ORDER BY agg_id)) t
        WHERE prev_id IS NOT NULL AND agg_id <> prev_id + 1 ORDER BY from_id""")
    r["agg_missing_ids"] = sum(max(0, g[1] - g[0] + 1) for g in r["agg_gaps"])
    r["trade_ts_inversions"] = one(f"""
        SELECT count(*) FROM (SELECT exchange_ts, lag(exchange_ts) OVER (ORDER BY agg_id) AS prev_ts
              FROM market.trades WHERE {_W_TRADES}) t WHERE exchange_ts < prev_ts""")

    silence = f"""
        FROM (SELECT exchange_ts, lag(exchange_ts) OVER (ORDER BY exchange_ts) AS prev_ts
              FROM {{table}} WHERE {_W_TRADES}) t
        WHERE exchange_ts - prev_ts > make_interval(secs => %({{gap}})s)"""
    for key, table, gap in (("trade", "market.trades", "tgap"), ("book", "market.book_top5", "bgap")):
        body = silence.format(table=table, gap=gap)
        r[f"{key}_silence_count"] = one(f"SELECT count(*) {body}")
        r[f"{key}_silences"] = rows(f"SELECT prev_ts, exchange_ts, exchange_ts - prev_ts AS dur {body} "
                                    f"ORDER BY dur DESC LIMIT %(limit)s")

    r["book_ts_inversions"] = one(f"""
        SELECT count(*) FROM (SELECT exchange_ts, lag(exchange_ts) OVER (ORDER BY update_id) AS prev_ts
              FROM market.book_top5 WHERE {_W_TRADES}) t WHERE exchange_ts < prev_ts""")
    r["book_crossed"] = one(f"SELECT count(*) FROM market.book_top5 WHERE {_W_TRADES} "
                            f"AND bid_px_1 >= ask_px_1")
    r["book_empty_top"] = one(f"SELECT count(*) FROM market.book_top5 WHERE {_W_TRADES} "
                              f"AND (bid_px_1 IS NULL OR ask_px_1 IS NULL)")

    r["latency_ms"] = rows(f"""
        SELECT percentile_cont(0.5) WITHIN GROUP (ORDER BY ms), percentile_cont(0.99) WITHIN GROUP (ORDER BY ms)
        FROM (SELECT extract(epoch FROM recv_ts - event_ts) * 1000 AS ms FROM market.trades
              WHERE {_W_TRADES} AND source = 'ws' AND recv_ts IS NOT NULL) t""")[0]
    r["events"] = rows("""
        SELECT kind, coalesce(stream, ''), coalesce(detail->>'event', ''), count(*)
        FROM market.collector_events
        WHERE recv_ts >= %(start)s AND recv_ts < %(end)s AND (symbol = %(symbol)s OR symbol IS NULL)
        GROUP BY 1, 2, 3 ORDER BY 1, 2, 3""")
    clock = rows("""SELECT (detail->>'offset_ms')::float FROM market.collector_events
                    WHERE kind = 'clock' AND recv_ts < %(end)s ORDER BY recv_ts DESC LIMIT 1""")
    r["clock_offset_ms"] = clock[0][0] if clock else None
    return r


def backfill_agg_gaps(conn: psycopg.Connection, rest, symbol: str, gaps: list[tuple],
                      max_ids_per_gap: int = 100_000) -> int:
    """aggTrade id 누락 구간을 REST 로 재수집해 source='rest_backfill' 로 적재."""
    inserted = 0
    for gap in gaps:
        from_id, to_id = int(gap[0]), int(gap[1])
        if to_id - from_id + 1 > max_ids_per_gap:
            log.warning("%s 누락 %d~%d 가 너무 커서 백필 생략", symbol, from_id, to_id)
            continue
        cur_id = from_id
        while cur_id <= to_id:
            batch = rest.get("/fapi/v1/aggTrades", {"symbol": symbol, "fromId": cur_id, "limit": 1000})
            if not batch:
                break
            rows = [trade_row(t, symbol=symbol, source="rest_backfill")
                    for t in batch if from_id <= int(t["a"]) <= to_id]
            if rows:
                with conn.cursor() as cur:
                    cur.executemany(TRADE_SQL, rows)
                inserted += len(rows)
            last = int(batch[-1]["a"])
            if last >= to_id or last < cur_id:
                break
            cur_id = last + 1
        conn.commit()
    return inserted


def _fmt_dur(td) -> str:
    return str(td).split(".")[0] if td is not None else "-"


def render_markdown(r: dict) -> tuple[str, dict]:
    """(본문, 요약 수치) 반환."""
    def mark(ok: bool, warn: bool = False) -> str:
        return "✅ 정상" if ok else ("⚠️ 경고" if warn else "❌ 이상")

    n_tr, n_bk = r["trades"][0], r["book_rows"][0]
    lat = r["latency_ms"]
    summary = {
        "결과_체결행": n_tr, "결과_5호가행": n_bk,
        "결과_체결id누락": r["agg_missing_ids"], "결과_체결id갭구간": len(r["agg_gaps"]),
        "결과_체결시각역전": r["trade_ts_inversions"], "결과_5호가시각역전": r["book_ts_inversions"],
        "결과_무체결구간": r["trade_silence_count"], "결과_5호가결측구간": r["book_silence_count"],
        "결과_호가교차": r["book_crossed"], "결과_최우선호가비어있음": r["book_empty_top"],
        "결과_수신지연ms_p50": None if lat[0] is None else round(lat[0], 1),
        "결과_수신지연ms_p99": None if lat[1] is None else round(lat[1], 1),
        "결과_시계오프셋ms": r["clock_offset_ms"],
    }
    if "backfilled" in r:
        summary["결과_백필행"] = r["backfilled"]

    lines = [
        f"- 심볼: `{r['symbol']}` / 구간: {r['start'].astimezone(KST):%Y-%m-%d %H:%M} ~ "
        f"{r['end'].astimezone(KST):%Y-%m-%d %H:%M} KST",
        f"- 체결 {n_tr:,}행 ({r['trades'][1]} ~ {r['trades'][2]}), 5호가 {n_bk:,}행",
        "",
        "| 검사 | 결과 | 상세 |",
        "|---|---|---|",
        f"| 데이터 존재 | {mark(n_tr > 0 and n_bk > 0)} | 체결 {n_tr:,} / 5호가 {n_bk:,} |",
        f"| 체결 id 연속성 | {mark(not r['agg_gaps'])} | 갭 {len(r['agg_gaps'])}구간, 누락 id {r['agg_missing_ids']:,}개"
        + (f", 백필 {r['backfilled']:,}행" if "backfilled" in r else "") + " |",
        f"| 체결 시각 역전 | {mark(r['trade_ts_inversions'] == 0)} | {r['trade_ts_inversions']}건 (agg_id 순서 기준) |",
        f"| 5호가 시각 역전 | {mark(r['book_ts_inversions'] == 0)} | {r['book_ts_inversions']}건 (update_id 순서 기준) |",
        f"| 무체결 구간 > {r['trade_gap_s']:.0f}s | {mark(r['trade_silence_count'] == 0, warn=True)} | {r['trade_silence_count']}구간 (저유동 시간대면 정상일 수 있음) |",
        f"| 5호가 결측 > {r['book_gap_s']:.0f}s | {mark(r['book_silence_count'] == 0, warn=True)} | {r['book_silence_count']}구간 |",
        f"| 호가 교차 (bid ≥ ask) | {mark(r['book_crossed'] == 0)} | {r['book_crossed']}행 |",
        f"| 최우선 호가 비어 있음 | {mark(r['book_empty_top'] == 0)} | {r['book_empty_top']}행 |",
        f"| 수신 지연 recv−E | ℹ️ | p50 {summary['결과_수신지연ms_p50']}ms / p99 {summary['결과_수신지연ms_p99']}ms, 시계 오프셋 {r['clock_offset_ms']}ms (+면 로컬이 빠름) |",
    ]
    for key, label in (("trade_silences", "무체결"), ("book_silences", "5호가 결측")):
        if r[key]:
            lines += ["", f"### 가장 긴 {label} 구간", "", "| 시작(KST) | 끝 | 길이 |", "|---|---|---|"]
            lines += [f"| {a.astimezone(KST):%Y-%m-%d %H:%M:%S} | {b.astimezone(KST):%H:%M:%S} | "
                      f"{_fmt_dur(d)} |" for a, b, d in r[key]]
    if r["agg_gaps"]:
        lines += ["", "### 체결 id 갭", "", "| from_id | to_id | 직전 시각(KST) | 다음 시각(KST) |",
                  "|---|---|---|---|"]
        lines += [f"| {g[0]} | {g[1]} | {g[2].astimezone(KST):%m-%d %H:%M:%S} | "
                  f"{g[3].astimezone(KST):%m-%d %H:%M:%S} |" for g in r["agg_gaps"][:20]]
    if r["events"]:
        lines += ["", "### 수집기 이벤트", "", "| kind | stream | event | 건수 |", "|---|---|---|---|"]
        lines += [f"| {k} | {s} | {e} | {c} |" for k, s, e, c in r["events"]]
    return "\n".join(lines), summary


def main() -> None:
    ap = argparse.ArgumentParser(description="적재 데이터 정합성 검사")
    ap.add_argument("--symbol", default="EWYUSDT")
    ap.add_argument("--hours", type=float, default=24.0)
    ap.add_argument("--start", default=None, help="UTC ISO 시각. 지정 시 --hours 무시")
    ap.add_argument("--end", default=None, help="UTC ISO 시각 (기본 현재)")
    ap.add_argument("--trade-gap", type=float, default=60.0)
    ap.add_argument("--book-gap", type=float, default=10.0)
    ap.add_argument("--backfill", action="store_true", help="체결 id 누락을 REST 로 재수집")
    ap.add_argument("--obsidian", action="store_true", help="결과를 실험 노트로 기록")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()
    setup_logging("INFO")

    vault = obsidian.testbed_root() if args.obsidian else None   # 기록 요청 시 경로 없으면 즉시 실패
    end = datetime.fromisoformat(args.end).astimezone(timezone.utc) if args.end else datetime.now(timezone.utc)
    start = (datetime.fromisoformat(args.start).astimezone(timezone.utc) if args.start
             else end - timedelta(hours=args.hours))

    with psycopg.connect(get_dsn(args.dsn)) as conn:
        r = run_checks(conn, args.symbol, start, end, args.trade_gap, args.book_gap)
        if args.backfill and r["agg_gaps"]:
            n = backfill_agg_gaps(conn, FuturesRestClient(), args.symbol, r["agg_gaps"])
            r = run_checks(conn, args.symbol, start, end, args.trade_gap, args.book_gap)
            r["backfilled"] = n
    body, summary = render_markdown(r)
    print(body)

    if vault is not None:
        meta = {"데이터구간_시작": start.isoformat(timespec="seconds"),
                "데이터구간_종료": end.isoformat(timespec="seconds"),
                "파라미터_심볼": args.symbol, "파라미터_무체결기준초": args.trade_gap,
                "파라미터_5호가결측기준초": args.book_gap, "파라미터_백필": args.backfill,
                **summary, "태그": ["crypto-testbed", "M2", "integrity"]}
        cmd = "python -m src.integrity " + " ".join(
            x for x in (f"--symbol {args.symbol}", f"--start {start.isoformat()}", f"--end {end.isoformat()}",
                        "--backfill" if args.backfill else "") if x)
        note = obsidian.write_experiment(f"정합성검사_{args.symbol}", meta,
                                         f"## 실행 명령\n\n```bash\n{cmd}\n```\n\n## 결과\n\n{body}",
                                         root=vault)
        obsidian.register_experiment_in_index(
            note, f"체결 {summary['결과_체결행']:,} · id누락 {summary['결과_체결id누락']} · "
                  f"역전 {summary['결과_체결시각역전']}/{summary['결과_5호가시각역전']} · 교차 {summary['결과_호가교차']}",
            root=vault)
        print(f"\nObsidian 기록: {note}")


if __name__ == "__main__":
    main()
