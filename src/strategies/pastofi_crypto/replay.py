"""DB 하루 재생: market.book_top5·market.trades 를 수집기 레코드 모양으로 바꿔 SymbolFeed(실시간과 같은 경로)에 흘린다.

시계: 이벤트를 거래소 시각 순으로 넣고, 시각 t 인 이벤트를 넣기 전에 T + decision_delay ≤ t 인 초 T를 모두 tick 한다
(실시간이 벽시계 T+0.3초에 판단하는 것과 같은 순서, 지연 0). 그날 끝에 86399까지 tick 하고 finish_day.

    python -m src.strategies.pastofi_crypto.replay --check          # 백테스트 A·로직1 59건 거래 단위 일치 검증
    python -m src.strategies.pastofi_crypto.replay --date 2026-10-13 --symbol BTCUSDT
"""
from __future__ import annotations

import argparse
import csv
import heapq
import os
from concurrent.futures import ProcessPoolExecutor
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .feed import DAY, Exit, Signal, SymbolFeed
from .spec import Spec

REPO = Path(__file__).resolve().parents[3]
BT_DIR = REPO / "research" / "pastofi_crypto"
KEYS = ("t_sig", "p_in", "p_out", "reason")


def _dsn() -> str:
    from ...storage.db import get_dsn
    return get_dsn(None)


def load_day(symbol: str, d: date, dsn: str | None = None) -> tuple[list, list]:
    """그날 [00:00, 24:00) UTC 호가(1호가)·체결. 시각은 epoch ms."""
    lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    return load_window(symbol, lo, lo + timedelta(days=1), dsn)


def load_window(symbol: str, lo: datetime, hi: datetime, dsn: str | None = None) -> tuple[list, list]:
    """[lo, hi) 호가(1호가)·체결. 시각은 epoch ms."""
    import psycopg
    with psycopg.connect(dsn or _dsn()) as conn:
        q = conn.execute(
            "SELECT (extract(epoch from exchange_ts) * 1000)::bigint, bid_px_1, bid_qty_1, ask_px_1, ask_qty_1 "
            "FROM market.book_top5 WHERE symbol = %s AND exchange_ts >= %s AND exchange_ts < %s "
            "ORDER BY exchange_ts, update_id", (symbol, lo, hi)).fetchall()
        t = conn.execute(
            "SELECT (extract(epoch from exchange_ts) * 1000)::bigint, qty, is_buyer_maker "
            "FROM market.trades WHERE symbol = %s AND exchange_ts >= %s AND exchange_ts < %s "
            "ORDER BY exchange_ts, agg_id", (symbol, lo, hi)).fetchall()
    return q, t


def run_day(spec: Spec, symbol: str, d: date, quotes: list, trades: list) -> list[dict]:
    """재생해서 로직1 거래(엔진 판단) 목록. 레코드는 수집기 싱크가 받는 모양 그대로 만든다."""
    day = (d - date(1970, 1, 1)).days
    out: list[dict] = []
    sigs: dict[int, Signal] = {}

    def on_entry(s: Signal) -> None:
        sigs[s.t_sig] = s

    def on_exit(x: Exit) -> None:
        tr = x.trade
        out.append(dict(sym=symbol, day=d.isoformat(), **tr.to_dict()))

    feed = SymbolFeed(spec, symbol, on_entry, on_exit)
    delay_ms = int(round(float(spec.raw["timing"]["decision_delay_s"]) * 1000))
    base_ms = day * DAY * 1000
    nxt = 0                                   # 다음에 tick 할 초
    qs = ((r[0], 0, r) for r in quotes)
    ts_ = ((r[0], 1, r) for r in trades)
    for ts, kind, r in heapq.merge(qs, ts_, key=lambda x: (x[0], x[1])):
        while nxt < DAY and base_ms + nxt * 1000 + delay_ms <= ts:
            feed.tick(day, nxt)
            nxt += 1
        if kind == 0:
            feed.on_book_top({"kind": "book_top", "symbol": symbol, "T": r[0], "E": r[0],
                              "bids": [[r[1], r[2]]], "asks": [[r[3], r[4]]]})
        else:
            feed.on_trade({"kind": "trade", "symbol": symbol, "T": r[0], "q": r[1], "m": r[2]})
    while nxt < DAY:
        feed.tick(day, nxt)
        nxt += 1
    feed.finish_day(day)
    return out


def replay(symbol: str, d: date, spec_path: str | None = None) -> list[dict]:
    spec = Spec.load(Path(spec_path)) if spec_path else Spec.load()
    q, t = load_day(symbol, d)
    if len(q) < 1000:                         # run_all.py 와 같다 (거의 빈 날은 건너뛴다)
        return []
    return run_day(spec, symbol, d, q, t)


# ---------------------------------------------------------------- 백테스트 일치 검증
def expected_trades() -> list[dict]:
    rows = []
    for f in sorted(BT_DIR.glob("all_trades_*.csv")):
        with f.open(encoding="utf-8") as fh:
            rows += [r for r in csv.DictReader(fh) if r["th"] == "A" and r["logic"] == "로직1"]
    return rows


def backtest_days() -> dict[str, list[date]]:
    """run_all.py 가 돌린 날: 종목별 DB 첫날·마지막날을 뺀 전부 (백테스트 CSV 만든 시점의 범위로 자른다)."""
    import psycopg
    end = date(2026, 10, 18)                  # 백테스트 마지막 날 (그 뒤는 표본 밖)
    out = {}
    with psycopg.connect(_dsn()) as conn:
        for sym in Spec.load().symbols:
            first = conn.execute("SELECT min(exchange_ts) FROM market.trades WHERE symbol = %s", (sym,)).fetchone()[0]
            d0 = first.astimezone(timezone.utc).date() + timedelta(days=1)
            out[sym] = [d0 + timedelta(days=i) for i in range((end - d0).days + 1)]
    return out


def _job(args):
    sym, d = args
    return sym, d, replay(sym, d)


def same(a: dict, b: dict) -> bool:
    return (int(float(a["t_sig"])) == int(float(b["t_sig"])) and abs(float(a["p_in"]) - float(b["p_in"])) < 1e-9
            and abs(float(a["p_out"]) - float(b["p_out"])) < 1e-9 and a["reason"] == b["reason"])


def compare(exp: list[dict], got: list[dict]) -> dict:
    key = lambda r: (r["sym"], r["day"], int(float(r["t_sig"])))
    e, g = {key(r): r for r in exp}, {key(r): r for r in got}
    match = [k for k in e if k in g and same(e[k], g[k])]
    diff = [k for k in e if k in g and not same(e[k], g[k])]
    return {"expected": len(e), "got": len(g), "match": len(match),
            "missing": sorted(k for k in e if k not in g), "extra": sorted(k for k in g if k not in e),
            "diff": sorted(diff), "ok": len(match) == len(e) == len(g)}


def check(workers: int | None = None) -> dict:
    days = backtest_days()
    jobs = [(s, d) for s, ds in days.items() for d in ds]
    got = []
    with ProcessPoolExecutor(max_workers=workers or min(8, os.cpu_count() or 4)) as ex:
        for sym, d, trades in ex.map(_job, jobs):
            got += trades
    return compare(expected_trades(), got) | {"days": len(jobs), "trades": got}


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.replay")
    ap.add_argument("--check", action="store_true", help="백테스트 A·로직1 59건 거래 단위 일치 검증")
    ap.add_argument("--date")
    ap.add_argument("--symbol")
    a = ap.parse_args(argv)
    if a.check:
        r = check()
        print(f"재생 {r['days']}종목·일 · 기대 {r['expected']}건 · 재생 {r['got']}건 · 일치 {r['match']}건")
        for k in ("missing", "extra", "diff"):
            if r[k]:
                print(f"  {k}: {r[k]}")
        print("✅ 일치" if r["ok"] else "❌ 불일치")
        raise SystemExit(0 if r["ok"] else 1)
    for t in replay(a.symbol, date.fromisoformat(a.date)):
        print({k: t[k] for k in ("t_sig", "p_in", "p_out", "reason", "past_bp", "ofi_ratio", "pnl_bp")})


if __name__ == "__main__":
    main()
