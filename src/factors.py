"""단기 알파 팩터와 선행 수익률의 상관 (백테스트 전 탐색용).

팩터 — 모두 시점 t 의 직전 lookback 초(기본 60s) 정보만 사용한다.
  TxnImbalance : (매수주도 체결량 - 매도주도 체결량) / 전체 체결량          [체결]
  LobImbalance : (매수 5호가 잔량합 - 매도 5호가 잔량합) / 전체 잔량 의
                 1초 LOCF 격자 평균 (= 시간가중 평균)                      [5호가]
  PastReturn   : mid(t) / mid(t - lookback) - 1                           [중간가]

타깃
  FwdReturn    : mid(t + horizon) / mid(t) - 1   (기본 10초 뒤)

5호가는 바뀔 때만 1행이 쌓이므로 1초 격자에 LOCF 로 채운다. 수집 중단 구간과
5호가가 max_stale 초 넘게 갱신되지 않은 구간은 창 [t-lookback, t+horizon] 이
온전한 시점만 남기고 버린다 (LOCF 가 만든 가짜 관측을 상관에 넣지 않기 위함).

  python -m src.factors                          # 전체 구간, 10초 격자
  python -m src.factors --hours 6 --csv results/factors.csv
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import psycopg

from .storage.db import get_dsn

KST = "Asia/Seoul"
FACTORS = ["TxnImbalance", "LobImbalance", "PastReturn"]
TARGET = "FwdReturn"

_QTY5 = "+".join(f"coalesce({{side}}_qty_{i}, 0)" for i in range(1, 6))


def load_book(conn: psycopg.Connection, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = conn.execute(
        f"SELECT exchange_ts, bid_px_1, ask_px_1, {_QTY5.format(side='bid')}, {_QTY5.format(side='ask')} "
        "FROM market.book_top5 WHERE symbol = %s AND exchange_ts >= %s AND exchange_ts < %s "
        "AND bid_px_1 IS NOT NULL AND ask_px_1 IS NOT NULL ORDER BY exchange_ts, update_id",
        (symbol, start, end)).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "bid_px", "ask_px", "bid_qty", "ask_qty"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_trades(conn: psycopg.Connection, symbol: str, start: datetime, end: datetime) -> pd.DataFrame:
    rows = conn.execute(
        "SELECT exchange_ts, qty, is_buyer_maker FROM market.trades "
        "WHERE symbol = %s AND exchange_ts >= %s AND exchange_ts < %s ORDER BY exchange_ts, agg_id",
        (symbol, start, end)).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "qty", "sell"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True)
    return df


def load_outages(conn: psycopg.Connection, start: datetime, end: datetime) -> list[tuple]:
    """수집 중단 구간 [(끊김, 복구), ...]. plot_trades 와 같은 규칙."""
    events = conn.execute(
        "SELECT recv_ts, detail->>'event' FROM market.collector_events "
        "WHERE kind = 'conn' AND recv_ts < %s ORDER BY recv_ts", (end,)).fetchall()
    outages, down_since = [], None
    for ts, ev in events:
        if ev in ("stopped", "disconnected") and down_since is None:
            down_since = ts
        elif ev == "connected" and down_since is not None:
            if ts > start:
                outages.append((max(down_since, start), ts))
            down_since = None
    if down_since is not None:
        outages.append((max(down_since, start), end))
    return [(pd.Timestamp(a).tz_convert("UTC"), pd.Timestamp(b).tz_convert("UTC")) for a, b in outages]


def build(book: pd.DataFrame, trades: pd.DataFrame, lookback: int = 60, horizon: int = 10,
          grid: int = 10, outages: list[tuple] = (), max_stale: float = 30.0) -> pd.DataFrame:
    """1초 격자에서 팩터·타깃을 만들고 grid 초 간격으로 표본을 뽑는다."""
    b = book.set_index("ts").sort_index()
    mid = (b.bid_px + b.ask_px) / 2
    imb = (b.bid_qty - b.ask_qty) / (b.bid_qty + b.ask_qty)

    sec = pd.DataFrame({"mid": mid.resample("1s").last(), "imb": imb.resample("1s").last()})
    fresh = sec.mid.notna()
    sec = sec.ffill()

    # 마지막 5호가 갱신 이후 경과 초 — LOCF 로 채운 구간이 얼마나 오래된 값인지
    pos = np.arange(len(sec), dtype=float)
    last_fresh = pd.Series(np.where(fresh, pos, np.nan), index=sec.index).ffill().to_numpy()
    stale_s = pos - last_fresh

    if trades.empty:
        buy = sell = pd.Series(0.0, index=sec.index)
    else:
        t = trades.set_index("ts").sort_index()
        buy = t.qty.where(~t.sell, 0.0).resample("1s").sum().reindex(sec.index, fill_value=0.0)
        sell = t.qty.where(t.sell, 0.0).resample("1s").sum().reindex(sec.index, fill_value=0.0)

    bw, sw = buy.rolling(lookback).sum(), sell.rolling(lookback).sum()
    tot = bw + sw
    df = pd.DataFrame({
        "mid": sec.mid,
        "TxnImbalance": ((bw - sw) / tot).where(tot > 0),   # 무체결 창은 방향 정보가 없음 → 제외
        "LobImbalance": sec.imb.rolling(lookback).mean(),
        "PastReturn": sec.mid / sec.mid.shift(lookback) - 1,
        TARGET: sec.mid.shift(-horizon) / sec.mid - 1,
    })

    bad = pd.Series(~np.isfinite(stale_s) | (stale_s > max_stale), index=sec.index)
    for a, z in outages:
        bad.loc[a:z] = True
    # 위치 i 의 rolling 은 [i-win, i] 을 덮는다. shift(-horizon) 으로 [t-lookback, t+horizon] 으로 옮긴다.
    win = lookback + horizon
    bad_win = bad.rolling(win + 1).max().shift(-horizon).fillna(1.0).astype(bool)

    # 초 환산은 인덱스 해상도(ns/us)에 의존하지 않도록 Timedelta 나눗셈으로 한다
    epoch = (df.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
    return df[~bad_win & (epoch % grid == 0)].dropna()


def corr_table(df: pd.DataFrame, cols: list[str] = FACTORS, target: str = TARGET) -> pd.DataFrame:
    """팩터별 Pearson/Spearman 상관과 t 값. 창이 겹치므로 t 값은 과대평가임에 주의."""
    out = []
    for c in cols:
        d = df[[c, target]].dropna()
        n = len(d)
        r = d[c].corr(d[target])
        rs = d[c].rank().corr(d[target].rank())   # Spearman = 순위의 Pearson (scipy 불필요)
        t = r * np.sqrt(max(n - 2, 1) / max(1 - r * r, 1e-12))
        out.append({"factor": c, "n": n, "pearson": r, "spearman": rs, "t_stat": t})
    return pd.DataFrame(out).set_index("factor")


def _fmt(df: pd.DataFrame) -> str:
    return df.to_string(float_format=lambda v: f"{v:+.4f}")


def main() -> None:
    ap = argparse.ArgumentParser(description="단기 팩터 ~ 선행수익률 상관")
    ap.add_argument("--symbol", default="EWYUSDT")
    ap.add_argument("--hours", type=float, default=None, help="최근 N시간 (기본: 수집 전체)")
    ap.add_argument("--lookback", type=int, default=60, help="팩터 룩백 (초)")
    ap.add_argument("--horizon", type=int, default=10, help="선행 수익률 구간 (초)")
    ap.add_argument("--grid", type=int, default=10, help="표본 간격 (초)")
    ap.add_argument("--max-stale", type=float, default=30.0, help="5호가 LOCF 허용 한계 (초)")
    ap.add_argument("--csv", default=None)
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    with psycopg.connect(get_dsn(args.dsn)) as conn:
        if args.hours:
            start = end - timedelta(hours=args.hours)
        else:
            start = conn.execute("SELECT min(exchange_ts) FROM market.book_top5 WHERE symbol = %s",
                                 (args.symbol,)).fetchone()[0]
            if start is None:
                raise SystemExit("5호가 데이터가 없습니다.")
        book = load_book(conn, args.symbol, start, end)
        trades = load_trades(conn, args.symbol, start, end)
        outages = load_outages(conn, start, end)
    if book.empty:
        raise SystemExit("구간 내 5호가 데이터가 없습니다.")

    df = build(book, trades, args.lookback, args.horizon, args.grid, outages, args.max_stale)
    if df.empty:
        raise SystemExit("유효 표본이 없습니다.")

    span = df.index.tz_convert(KST)
    print(f"{args.symbol} {span[0]:%Y-%m-%d %H:%M} ~ {span[-1]:%m-%d %H:%M} KST "
          f"(체결 {len(trades):,} · 5호가 {len(book):,} · 수집중단 {len(outages)}건)")
    print(f"룩백 {args.lookback}s · 선행 {args.horizon}s · 격자 {args.grid}s → 표본 {len(df):,}\n")

    print(f"[팩터 ~ {TARGET}({args.horizon}s)]")
    print(_fmt(corr_table(df)))

    step = max(1, (args.lookback + args.horizon) // args.grid)
    ind = df.iloc[::step]
    print(f"\n[창 비중첩 표본 {len(ind):,}개 ({step * args.grid}s 간격)]")
    print(_fmt(corr_table(ind)))

    print("\n[팩터 간 상관]")
    print(_fmt(df[FACTORS].corr()))

    print("\n[팩터 분포]")
    print(_fmt(df[FACTORS + [TARGET]].describe().loc[["mean", "std", "min", "50%", "max"]]))

    if args.csv:
        out = Path(args.csv)
        out.parent.mkdir(parents=True, exist_ok=True)
        df.to_csv(out)
        print(f"\n{out}")


if __name__ == "__main__":
    main()
