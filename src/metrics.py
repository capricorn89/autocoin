"""성과 지표 및 리포트(그래프) 생성."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

RESULTS_DIR = Path(__file__).resolve().parent.parent / "results"


def max_drawdown(equity: pd.Series) -> float:
    if equity.empty:
        return 0.0
    running_max = equity.cummax()
    dd = equity - running_max
    return float(dd.min())


def sharpe(returns: pd.Series, periods_per_year: float) -> float:
    r = returns.dropna()
    if r.std() == 0 or r.empty:
        return 0.0
    return float(r.mean() / r.std() * np.sqrt(periods_per_year))


def summarize(equity: pd.Series, cycles: pd.DataFrame, bars_per_year: float) -> dict:
    rets = equity.diff()
    out = {
        "복제_총손익": float(equity.iloc[-1] - equity.iloc[0]) if len(equity) else 0.0,
        "복제_MDD": max_drawdown(equity),
        "복제_Sharpe": sharpe(rets, bars_per_year),
        "사이클수": int(len(cycles)),
    }
    if not cycles.empty:
        out["이론옵션_총손익"] = float(cycles["opt_pnl"].sum())
        out["복제_사이클합"] = float(cycles["repl_pnl"].sum())
        out["복제오차(복제-이론)"] = out["복제_사이클합"] - out["이론옵션_총손익"]
        out["평균_프리미엄"] = float(cycles["premium"].mean())
        out["평균_페이오프"] = float(cycles["payoff"].mean())
    return out


def save_report(name: str, df: pd.DataFrame, cycles: pd.DataFrame, summary: dict) -> dict:
    RESULTS_DIR.mkdir(exist_ok=True)
    paths = {}
    eq_csv = RESULTS_DIR / f"{name}_equity.csv"
    cy_csv = RESULTS_DIR / f"{name}_cycles.csv"
    df.to_csv(eq_csv)
    cycles.to_csv(cy_csv, index=False)
    paths["equity_csv"] = str(eq_csv)
    paths["cycles_csv"] = str(cy_csv)

    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        from matplotlib import font_manager

        # 한글 폰트(있으면) 적용 — macOS: AppleGothic, 그 외 가용 폰트 탐색
        installed = {f.name for f in font_manager.fontManager.ttflist}
        for cand in ("AppleGothic", "Apple SD Gothic Neo", "NanumGothic",
                     "Malgun Gothic", "Noto Sans CJK KR"):
            if cand in installed:
                plt.rcParams["font.family"] = cand
                break
        plt.rcParams["axes.unicode_minus"] = False

        fig, axes = plt.subplots(3, 1, figsize=(12, 11), sharex=True)
        axes[0].plot(df.index, df["equity"], label="복제 자산(선물)", color="C0")
        if "theo_cum" in df:
            axes[0].plot(df.index, df["theo_cum"], label="이론 옵션 누적손익",
                         color="C3", alpha=0.8)
        axes[0].set_title("복제 vs 이론 스트래들 누적손익")
        axes[0].legend(); axes[0].grid(alpha=0.3)

        axes[1].plot(df.index, df["price"], color="C1", label="EWYUSDT")
        if "K" in df:
            axes[1].plot(df.index, df["K"], color="C7", ls="--", lw=0.8, label="행사가 K")
        axes[1].set_title("가격 / 행사가"); axes[1].legend(); axes[1].grid(alpha=0.3)

        axes[2].plot(df.index, df["position"], color="C2")
        axes[2].axhline(0, color="k", lw=0.6)
        axes[2].set_title("선물 포지션(=스트래들 델타)"); axes[2].grid(alpha=0.3)

        fig.tight_layout()
        png = RESULTS_DIR / f"{name}_report.png"
        fig.savefig(png, dpi=110)
        plt.close(fig)
        paths["report_png"] = str(png)
    except Exception as e:  # 그래프 실패해도 csv 는 남김
        paths["plot_error"] = str(e)

    return paths
