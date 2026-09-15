"""리밸런싱 파라미터 스윕: rebalance_interval × rebalance_band 격자 탐색.

데이터를 1회만 로드하고, (주기, 밴드) 조합마다 시뮬레이션을 돌려
수수료 vs 복제오차(트래킹 에러) 트레이드오프를 한 표로 비교한다.

--splits N (>1) 을 주면 walk-forward 검증: 기간을 N개 연속 구간으로 쪼개
구간마다 같은 격자를 돌리고, 조합별 성과가 구간 전반에서 일관적인지(과적합
아닌지) 평균/표준편차/최악구간으로 보여준다.

예)
  python -m src.sweep --intervals 1m,5m,15m,1h --bands 0,0.05,0.1,0.2
  python -m src.sweep --intervals 15m --bands 0,0.05,0.1,0.15,0.2,0.3 --splits 4
"""
from __future__ import annotations

import argparse
from dataclasses import replace

import numpy as np
import pandas as pd

from src import binance_data as bd
from .backtest import load_data, simulate
from src.config import Config, load_config, parse_interval
from src.metrics import RESULTS_DIR, summarize


def _parse_list(text: str) -> list[str]:
    return [t.strip() for t in str(text).split(",") if t.strip()]


def _grid(cfg: Config, df: pd.DataFrame, vol_series: pd.Series,
          intervals: list[str], bands: list[float], verbose: bool = True) -> list[dict]:
    """주어진 데이터 구간에서 (주기×밴드) 격자를 돌려 조합별 요약 행들을 반환."""
    data_secs = bd._INTERVAL_MS[cfg.backtest.interval] / 1000
    bars_per_year = bd._INTERVAL_MS["1d"] / bd._INTERVAL_MS[cfg.backtest.interval] * 365

    rows = []
    for itv in intervals:
        # 데이터 해상도보다 잦은 리밸런스는 효과 없음(같은 봉에서 1회만) — 경고만
        note = " (데이터 해상도보다 촘촘 → 무효)" if parse_interval(itv) < data_secs else ""
        for band in bands:
            trial = replace(cfg, rebalance_interval=itv, rebalance_band=float(band))
            out, cycles_df, broker = simulate(trial, df, vol_series)
            s = summarize(out["equity"], cycles_df, bars_per_year)
            rows.append({
                "interval": itv,
                "band": float(band),
                "복제_총손익": s["복제_총손익"],
                "복제오차(복제-이론)": s.get("복제오차(복제-이론)", float("nan")),
                "수수료_총액": broker.fees_paid,
                "거래_횟수": len(broker.trades),
                "복제_MDD": s["복제_MDD"],
                "복제_Sharpe": s["복제_Sharpe"],
            })
            if verbose:
                print(f"      interval={itv:<4} band={band:<5} "
                      f"손익={s['복제_총손익']:>10.2f}  수수료={broker.fees_paid:>9.2f}  "
                      f"거래={len(broker.trades):>5}{note}")
    return rows


def run_sweep(cfg: Config, intervals: list[str], bands: list[float]) -> pd.DataFrame:
    """전체 기간 단일 격자 스윕. 조합별 요약 지표 DataFrame 반환."""
    # 데이터·변동성은 파라미터와 무관 → 1회 로드 후 재사용
    df, vol_series = load_data(cfg)
    print(f"[3/4] 스윕 {len(intervals) * len(bands)}개 조합 시뮬레이션...")
    rows = _grid(cfg, df, vol_series, intervals, bands)
    # 트래킹 에러(절대값) 작을수록, 같은 오차면 거래 적을수록 선호
    return pd.DataFrame(rows).sort_values("거래_횟수").reset_index(drop=True)


def run_walkforward(cfg: Config, intervals: list[str], bands: list[float],
                    n_splits: int) -> tuple[pd.DataFrame, pd.DataFrame]:
    """기간을 n_splits 개 연속 구간으로 쪼개 각 구간에서 격자 스윕.

    반환: (long_df, agg)
      long_df: 구간×조합 원시 결과 (split, 기간, interval, band, 지표들)
      agg    : 조합별 구간 전반 집계 (손익 평균/표준편차/최악, 오차 평균, 거래 평균)
    """
    df, vol_series = load_data(cfg)
    bounds = np.linspace(0, len(df), n_splits + 1).astype(int)

    print(f"[3/4] walk-forward {n_splits}구간 × {len(intervals) * len(bands)}조합 시뮬레이션...")
    all_rows = []
    for i in range(n_splits):
        sl = df.iloc[bounds[i]:bounds[i + 1]]
        label = f"{sl.index[0].date()}~{sl.index[-1].date()}"
        print(f"  [구간 {i + 1}/{n_splits}] {label}  ({len(sl)}봉)")
        for r in _grid(cfg, sl, vol_series, intervals, bands, verbose=False):
            r["split"] = i
            r["기간"] = label
            all_rows.append(r)

    long_df = pd.DataFrame(all_rows)
    g = long_df.groupby(["interval", "band"])
    agg = pd.DataFrame({
        "손익_평균": g["복제_총손익"].mean(),
        "손익_표준편차": g["복제_총손익"].std(),
        "손익_최악구간": g["복제_총손익"].min(),
        "오차절대_평균": g["복제오차(복제-이론)"].apply(lambda s: s.abs().mean()),
        "거래_평균": g["거래_횟수"].mean(),
    }).reset_index()
    # 평균 좋고(↑) 변동 작은(↓) 조합 선호 — 평균 내림차순 정렬
    agg = agg.sort_values("손익_평균", ascending=False).reset_index(drop=True)
    return long_df, agg


def _print_df(df: pd.DataFrame) -> None:
    print(df.to_string(index=False))


def main():
    ap = argparse.ArgumentParser(description="리밸런싱 파라미터 스윕(주기×밴드)")
    ap.add_argument("--config", default="legacy/straddle/config.yaml")
    ap.add_argument("--intervals", default="1m,5m,15m,1h",
                    help="리밸런스 주기 격자(쉼표구분). 예: 1m,5m,15m,1h")
    ap.add_argument("--bands", default="0,0.05,0.1,0.2",
                    help="no-trade 밴드 격자(쉼표구분, 델타 단위). 예: 0,0.05,0.1")
    ap.add_argument("--splits", type=int, default=1,
                    help="walk-forward 구간 수. 1=전체기간 단일 스윕(기본), >=2=구간 일관성 검증")
    ap.add_argument("--dte", type=int, default=None, metavar="N",
                    help="만기 오프셋(일). 0=당일 0DTE, 1=익일 1DTE. config.yaml의 "
                         "expiry_offset_days를 덮어씀")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                    help="백테스트 시작일 (UTC). 예: 2026-05-28")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                    help="백테스트 종료일 (UTC). 예: 2026-06-28")
    ap.add_argument("--maker", action="store_true",
                    help="지정가(maker) 체결 가정: 슬리피지 0, 수수료 maker_fee_bps 적용")
    args = ap.parse_args()

    cfg = load_config(args.config)
    if args.maker:
        cfg = replace(cfg, taker_fee_bps=cfg.maker_fee_bps, slippage_bps=0.0)
    if args.start is not None or args.end is not None:
        bt = replace(cfg.backtest,
                     start=args.start if args.start is not None else cfg.backtest.start,
                     end=args.end if args.end is not None else cfg.backtest.end)
        cfg = replace(cfg, backtest=bt)
    if args.dte is not None:
        cfg = replace(cfg, expiry_offset_days=args.dte)
        cfg.validate()
    intervals = _parse_list(args.intervals)
    bands = [float(b) for b in _parse_list(args.bands)]

    pd.set_option("display.float_format", lambda v: f"{v:,.2f}")
    pd.set_option("display.max_rows", None)
    pd.set_option("display.width", 200)
    RESULTS_DIR.mkdir(exist_ok=True)

    if args.splits <= 1:
        res = run_sweep(cfg, intervals, bands)
        print("[4/4] 결과 저장...")
        out_csv = RESULTS_DIR / "sweep.csv"
        res.to_csv(out_csv, index=False)
        print("\n===== 스윕 결과 (거래 횟수 오름차순) =====")
        _print_df(res)
        print(f"\n저장: {out_csv}")
        print("\n해석: '복제오차(복제-이론)' 절대값이 작을수록 헤지 정확, "
              "'수수료_총액'·'거래_횟수' 작을수록 비용 적음. 둘의 균형점을 고르세요.")
        return

    long_df, agg = run_walkforward(cfg, intervals, bands, args.splits)
    print("[4/4] 결과 저장...")
    long_csv = RESULTS_DIR / "sweep_walkforward.csv"
    agg_csv = RESULTS_DIR / "sweep_walkforward_agg.csv"
    long_df.to_csv(long_csv, index=False)
    agg.to_csv(agg_csv, index=False)

    # 구간별 손익 피벗 — 한눈에 일관성 확인
    pivot = long_df.pivot_table(index=["interval", "band"], columns="기간",
                                values="복제_총손익")
    print("\n===== 구간별 복제_총손익 (행=조합, 열=기간) =====")
    _print_df(pivot.reset_index())
    print("\n===== 조합별 집계 (손익_평균 내림차순) =====")
    _print_df(agg)
    print(f"\n저장: {long_csv}\n      {agg_csv}")
    print("\n해석: '손익_평균' 높고 '손익_표준편차' 작으며 '손익_최악구간'이 견딜 만한 "
          "조합이 강건합니다. 특정 구간에서만 1등인 밴드는 과적합 신호 — 모든 구간에서 "
          "꾸준히 상위인 밴드를 고르세요.")


if __name__ == "__main__":
    main()
