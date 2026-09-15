"""체결 데이터 차트: 체결가 + 구간별 매수/매도 주도 체결량.

  python -m src.plot_trades                         # EWYUSDT 최근 60분 → results/trades_chart.png
  python -m src.plot_trades --minutes 240 --bucket 1min

 - 두 측정값(가격, 체결량)은 축 하나에 겹치지 않고 위아래 두 패널로 나눈다.
 - 매수 주도(m=false)는 0 위, 매도 주도(m=true)는 0 아래로 그려 방향(극성)을 드러낸다.
 - 수집기가 멈췄던 구간(collector_events 의 stopped/disconnected → connected)은 회색으로 표시하고
   가격 선을 잇지 않는다 (체결이 없던 것과 수집하지 않은 것을 구분).
"""
from __future__ import annotations

import argparse
from datetime import datetime, timedelta, timezone
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
import psycopg  # noqa: E402
from matplotlib import font_manager  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402

from .storage.db import get_dsn  # noqa: E402

# 차트 팔레트 (dataviz 기본 팔레트, 라이트 모드)
SURFACE, INK, INK2, MUTED = "#fcfcfb", "#0b0b0b", "#52514e", "#898781"
GRID, AXIS, NEUTRAL = "#e1e0d9", "#c3c2b7", "#f0efec"
BUY, SELL = "#2a78d6", "#e34948"      # 극성: blue ↔ red
PRICE = "#2a78d6"
KST = "Asia/Seoul"


def load(conn, symbol: str, start: datetime, end: datetime) -> tuple[pd.DataFrame, list]:
    rows = conn.execute(
        "SELECT exchange_ts, price, qty, is_buyer_maker FROM market.trades "
        "WHERE symbol = %s AND exchange_ts >= %s AND exchange_ts < %s ORDER BY exchange_ts, agg_id",
        (symbol, start, end)).fetchall()
    df = pd.DataFrame(rows, columns=["ts", "price", "qty", "sell"])
    df["ts"] = pd.to_datetime(df["ts"], utc=True).dt.tz_convert(KST)

    events = conn.execute(
        "SELECT recv_ts, detail->>'event' FROM market.collector_events "
        "WHERE kind = 'conn' AND stream = 'trade' AND recv_ts < %s ORDER BY recv_ts", (end,)).fetchall()
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
    outages = [(pd.Timestamp(a).tz_convert(KST), pd.Timestamp(b).tz_convert(KST)) for a, b in outages]
    return df, outages


def _style(ax) -> None:
    ax.set_facecolor(SURFACE)
    for side in ("top", "right"):
        ax.spines[side].set_visible(False)
    for side in ("left", "bottom"):
        ax.spines[side].set_color(AXIS)
        ax.spines[side].set_linewidth(1)
    ax.tick_params(colors=MUTED, labelsize=9, length=0)
    ax.grid(axis="y", color=GRID, linewidth=1)
    ax.set_axisbelow(True)


def plot(df: pd.DataFrame, outages: list, symbol: str, bucket: str, out: Path) -> Path:
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("Apple SD Gothic Neo", "AppleGothic", "NanumGothic", "Noto Sans CJK KR"):
        if cand in installed:
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False

    fig, (ax1, ax2) = plt.subplots(2, 1, sharex=True, figsize=(12, 7.2), facecolor=SURFACE,
                                   gridspec_kw={"height_ratios": [3, 2], "hspace": 0.28})
    for ax in (ax1, ax2):
        _style(ax)
        for a, b in outages:
            ax.axvspan(a, b, color=NEUTRAL, linewidth=0, zorder=0)

    # 1) 체결가 — 수집 중단 구간에서는 선을 끊는다
    s = df.set_index("ts")["price"]
    for a, _ in outages:
        s.loc[a] = np.nan
    s = s.sort_index()
    ax1.step(s.index, s.values, where="post", color=PRICE, linewidth=1.5, solid_joinstyle="round")
    last = df.iloc[-1]
    ax1.plot([last.ts], [last.price], "o", color=PRICE, markersize=6,
             markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=3)
    ax1.annotate(f"{last.price:,.2f}", (last.ts, last.price), xytext=(8, 0),
                 textcoords="offset points", va="center", color=INK, fontsize=10)
    lo, hi = df.price.min(), df.price.max()
    pad = max((hi - lo) * 0.15, 0.02)
    ax1.set_ylim(lo - pad, hi + pad)
    ax1.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{v:,.2f}"))
    ax1.set_title(f"{symbol} 체결가", loc="left", color=INK, fontsize=13, pad=22)
    ax1.text(0, 1.02, f"{df.ts.iloc[0]:%Y-%m-%d %H:%M:%S} ~ {df.ts.iloc[-1]:%H:%M:%S} KST · 체결 {len(df):,}건 · "
                      f"저가 {lo:,.2f} / 고가 {hi:,.2f}",
             transform=ax1.transAxes, color=INK2, fontsize=9)
    for a, b in outages:
        ax1.text(a + (b - a) / 2, 0.97, "수집 중단", transform=ax1.get_xaxis_transform(),
                 ha="center", va="top", color=MUTED, fontsize=8.5)

    # 2) 구간별 매수/매도 주도 체결량 (0 기준 위/아래)
    g = df.set_index("ts").groupby([pd.Grouper(freq=bucket), "sell"])["qty"].sum().unstack(fill_value=0.0)
    buy = g.get(False, pd.Series(0.0, index=g.index))
    sell = g.get(True, pd.Series(0.0, index=g.index))
    width = pd.Timedelta(bucket) * 0.72
    offset = pd.Timedelta(bucket) / 2
    ax2.bar(g.index + offset, buy.values, width=width, color=BUY, label="매수 주도 체결량", linewidth=0)
    ax2.bar(g.index + offset, -sell.values, width=width, color=SELL, label="매도 주도 체결량", linewidth=0)
    ax2.axhline(0, color=AXIS, linewidth=1)
    lim = max(buy.max(), sell.max()) * 1.15 or 1
    ax2.set_ylim(-lim, lim)
    ax2.yaxis.set_major_formatter(FuncFormatter(lambda v, _: f"{abs(v):,.0f}"))
    ax2.set_title(f"{pd.Timedelta(bucket).seconds}초 단위 체결량 (EWY 계약 수)", loc="left",
                  color=INK, fontsize=11, pad=24)
    leg = ax2.legend(loc="lower left", bbox_to_anchor=(0, 1.0), ncol=2, frameon=False, fontsize=9,
                     handlelength=1.0, handleheight=0.8, borderaxespad=0.2, columnspacing=1.6)
    for t in leg.get_texts():
        t.set_color(INK2)
    tb, ts_ = buy.sum(), sell.sum()
    ax2.text(1, 1.03, f"합계 매수 {tb:,.1f} · 매도 {ts_:,.1f}", transform=ax2.transAxes,
             ha="right", color=INK2, fontsize=9)

    ax2.xaxis.set_major_formatter(mdates.DateFormatter("%H:%M", tz=df.ts.dt.tz))
    ax2.set_xlabel("시각 (KST)", color=MUTED, fontsize=9)
    ax2.set_xlim(df.ts.iloc[0] - pd.Timedelta(bucket), df.ts.iloc[-1] + pd.Timedelta(bucket) * 2)

    out.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(out, dpi=150, bbox_inches="tight", facecolor=SURFACE)
    plt.close(fig)
    return out


def main() -> None:
    ap = argparse.ArgumentParser(description="체결 데이터 차트")
    ap.add_argument("--symbol", default="EWYUSDT")
    ap.add_argument("--minutes", type=float, default=60)
    ap.add_argument("--bucket", default="10s", help="체결량 집계 단위 (예: 10s, 1min)")
    ap.add_argument("--out", default="results/trades_chart.png")
    ap.add_argument("--dsn", default=None)
    args = ap.parse_args()

    end = datetime.now(timezone.utc)
    start = end - timedelta(minutes=args.minutes)
    with psycopg.connect(get_dsn(args.dsn)) as conn:
        df, outages = load(conn, args.symbol, start, end)
    if df.empty:
        raise SystemExit("구간 내 체결 데이터가 없습니다.")
    print(plot(df, outages, args.symbol, args.bucket, Path(args.out)))


if __name__ == "__main__":
    main()
