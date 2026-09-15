"""백테스트: 과거 1m 데이터로 복제 엔진을 구동.

라이브와 동일한 StraddleReplicator + PaperBroker 를 사용한다.
사이클별로 이론 스트래들 손익(페이오프-프리미엄)과 복제 손익을 비교한다.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import timezone

import numpy as np
import pandas as pd

from src import binance_data as bd
from src.broker import PaperBroker
from src.config import Config, load_config
from src.metrics import save_report, summarize
from .strategy import StraddleReplicator
from .straddle import straddle_price


def _sigma_lookup(cfg: Config, vol_series: pd.Series, ts) -> float:
    """ts 시점에 사용할 연율 변동성 (lookahead 방지: 직전 일자 값)."""
    if cfg.vol_mode == "fixed":
        return cfg.vol_value
    day = pd.Timestamp(ts).normalize()
    prior = vol_series.loc[vol_series.index < day]
    if len(prior) and not np.isnan(prior.iloc[-1]):
        return float(prior.iloc[-1])
    return cfg.vol_value  # 초기 히스토리 부족 시 폴백


def load_data(cfg: Config, verbose: bool = True) -> tuple[pd.DataFrame, pd.Series]:
    """가격 봉 + 실현변동성 시리즈를 로드(캐시 우선). 스윕 등에서 1회만 호출해 재사용."""
    interval = cfg.backtest.interval
    start_ms = None
    end_ms = None
    if cfg.backtest.start:
        start_ms = int(pd.Timestamp(cfg.backtest.start, tz="UTC").timestamp() * 1000)
    if cfg.backtest.end:
        end_ms = int(pd.Timestamp(cfg.backtest.end, tz="UTC").timestamp() * 1000)

    if verbose:
        print(f"[1/4] {cfg.symbol} {interval} 데이터 로드 중...")
    df = bd.load_klines(cfg.symbol, interval, start_ms, end_ms)
    if df.empty:
        raise SystemExit("데이터가 비어 있습니다. 네트워크/심볼을 확인하세요.")
    if verbose:
        print(f"      {len(df)} 봉  ({df.index[0]} ~ {df.index[-1]})")
        gaps = bd.find_gaps(df, interval)
        if not gaps.empty:
            print(f"      데이터 갭 {len(gaps)}건(휴장/주말 추정) — 직전 유효가로 K 설정됨")

    # 실현변동성용 일봉
    vol_series = pd.Series(dtype=float)
    if cfg.vol_mode == "realized":
        if verbose:
            print("[2/4] 일봉 기반 실현변동성 계산...")
        daily = bd.load_klines(cfg.symbol, "1d", start_ms, end_ms)
        vol_series = bd.realized_vol_series(daily["close"], cfg.vol_window)
        vol_series.index = vol_series.index.normalize()
    elif verbose:
        print(f"[2/4] 고정 변동성 사용: σ={cfg.vol_value}")
    return df, vol_series


def simulate(cfg: Config, df: pd.DataFrame, vol_series: pd.Series):
    """주어진 데이터로 복제 엔진을 구동. (records_df, cycles_df, broker) 반환.

    데이터 로드/리포팅과 분리된 순수 시뮬레이션 코어 — 백테스트와 스윕이 공유한다.
    """
    eng = StraddleReplicator(cfg)
    broker = PaperBroker(taker_fee_bps=cfg.taker_fee_bps, slippage_bps=cfg.slippage_bps)

    records = []
    cycles = []
    cur = None  # 진행 중 사이클 정보
    theo_cum = 0.0  # 이론 옵션 누적손익(확정분)

    for ts, row in zip(df.index, df["close"].to_numpy()):
        price = float(row)
        ts = ts.to_pydatetime().astimezone(timezone.utc)
        sigma = _sigma_lookup(cfg, vol_series, ts)
        dec = eng.step(ts, price, sigma)

        # 만기/롤 정산 (price = 만기 시점 S_T)
        if dec.settled and cur is not None:
            payoff = abs(price - cur["K"]) * cfg.contracts
            opt_pnl = payoff - cur["premium"]
            theo_cum += opt_pnl
            repl_pnl = broker.equity(price) - cur["equity_start"]
            cycles.append({
                "anchor": cur["anchor"], "K": cur["K"],
                "S_expiry": price, "premium": cur["premium"],
                "payoff": payoff, "opt_pnl": opt_pnl, "repl_pnl": repl_pnl,
            })
            cur = None

        # 신규 사이클 진입
        if dec.new_cycle:
            premium = eng.entry_premium(price, sigma)
            cur = {"anchor": eng.anchor, "K": eng.K, "premium": premium,
                   "equity_start": broker.equity(price)}

        if dec.is_rebalance:
            broker.rebalance_to(ts, dec.target_position, price,
                                band=cfg.rebalance_band,
                                reason="reset" if dec.new_cycle else
                                       ("settle" if dec.settled else "rebal"))

        # flat 구간 미실현 이론손익은 0
        unreal = (abs(price - cur["K"]) * cfg.contracts - cur["premium"]) if (cur and dec.active) else 0.0
        records.append({
            "ts": ts, "price": price, "K": dec.K, "tau": dec.tau, "sigma": sigma,
            "target": dec.target_position, "position": broker.position,
            "active": dec.active, "equity": broker.equity(price),
            "theo_cum": theo_cum + unreal,
        })

    out = pd.DataFrame(records).set_index("ts")
    cycles_df = pd.DataFrame(cycles)
    return out, cycles_df, broker


def run_backtest(cfg: Config) -> dict:
    df, vol_series = load_data(cfg)

    print("[3/4] 복제 시뮬레이션...")
    out, cycles_df, broker = simulate(cfg, df, vol_series)

    bars_per_year = bd._INTERVAL_MS["1d"] / bd._INTERVAL_MS[cfg.backtest.interval] * 365
    summary = summarize(out["equity"], cycles_df, bars_per_year)
    summary["수수료_총액"] = broker.fees_paid
    summary["거래_횟수"] = len(broker.trades)

    print("[4/4] 리포트 저장...")
    paths = save_report("backtest", out, cycles_df, summary)

    print("\n===== 백테스트 요약 =====")
    for k, v in summary.items():
        print(f"  {k}: {v:,.4f}" if isinstance(v, float) else f"  {k}: {v}")
    print("\n생성 파일:")
    for k, v in paths.items():
        print(f"  {k}: {v}")
    return {"summary": summary, "paths": paths}


def run_compare_dte(cfg: Config, dte_list: list[int] | None = None) -> dict:
    """0DTE/1DTE(또는 지정 DTE 목록)를 같은 데이터에서 비교한다.

    데이터를 1회만 로드하고, 각 DTE 설정으로 simulate()를 독립 실행한다.
    """
    if dte_list is None:
        dte_list = [0, 1]
    df, vol_series = load_data(cfg)
    bars_per_year = bd._INTERVAL_MS["1d"] / bd._INTERVAL_MS[cfg.backtest.interval] * 365

    print(f"[3/4] DTE 비교 시뮬레이션 ({', '.join(f'{d}DTE' for d in dte_list)})...")
    results = {}
    for dte in dte_list:
        trial = replace(cfg, expiry_offset_days=dte)
        trial.validate()
        out, cycles_df, broker = simulate(trial, df, vol_series)
        s = summarize(out["equity"], cycles_df, bars_per_year)
        s["수수료_총액"] = broker.fees_paid
        s["거래_횟수"] = len(broker.trades)
        results[dte] = {"summary": s, "out": out, "cycles_df": cycles_df, "broker": broker}

    print("[4/4] 리포트 저장...")
    paths_all = {}
    for dte in dte_list:
        p = save_report(f"backtest_{dte}dte", results[dte]["out"],
                        results[dte]["cycles_df"], results[dte]["summary"])
        paths_all[dte] = p

    # 나란히 비교표 출력
    keys = list(next(iter(results.values()))["summary"].keys())
    col_w = 16
    label_w = 28
    header = f"{'항목':<{label_w}}" + "".join(f"{'%dDTE' % d:>{col_w}}" for d in dte_list)
    sep = "-" * len(header)
    print(f"\n===== DTE 비교: {' vs '.join(f'{d}DTE' for d in dte_list)} =====")
    print(header)
    print(sep)
    for k in keys:
        row = f"{k:<{label_w}}"
        for d in dte_list:
            v = results[d]["summary"].get(k, float("nan"))
            row += f"{v:>{col_w},.4f}" if isinstance(v, float) else f"{v:>{col_w}}"
        print(row)
    print()
    for dte in dte_list:
        print(f"{dte}DTE 파일: " + ", ".join(str(v) for v in paths_all[dte].values()))

    return {dte: {"summary": results[dte]["summary"], "paths": paths_all[dte]}
            for dte in dte_list}


def main():
    ap = argparse.ArgumentParser(description="EWYUSDT 스트래들 복제 백테스트")
    ap.add_argument("--config", default="legacy/straddle/config.yaml")
    ap.add_argument("--dte", type=int, default=None, metavar="N",
                    help="만기 오프셋(일). 0=당일 0DTE, 1=익일 1DTE. config.yaml의 "
                         "expiry_offset_days를 덮어씀")
    ap.add_argument("--compare-dte", nargs="*", type=int, default=None, metavar="N",
                    dest="compare_dte",
                    help="DTE 목록을 나란히 비교. 인수 없으면 '0 1'. "
                         "예: --compare-dte  또는  --compare-dte 0 1 2")
    ap.add_argument("--start", default=None, metavar="YYYY-MM-DD",
                    help="백테스트 시작일 (UTC). 예: 2026-05-28")
    ap.add_argument("--end", default=None, metavar="YYYY-MM-DD",
                    help="백테스트 종료일 (UTC). 예: 2026-06-28")
    ap.add_argument("--interval", default=None, metavar="INTERVAL",
                    help="리밸런스 주기. 예: 30m, 1h. config.yaml의 rebalance_interval을 덮어씀")
    ap.add_argument("--band", type=float, default=None, metavar="BAND",
                    help="리밸런스 밴드(델타 단위). 예: 0.5. config.yaml의 rebalance_band를 덮어씀")
    ap.add_argument("--maker", action="store_true",
                    help="지정가(maker) 체결 가정: 슬리피지 0, 수수료 maker_fee_bps 적용")
    args = ap.parse_args()
    cfg = load_config(args.config)

    if args.start is not None or args.end is not None:
        bt = replace(cfg.backtest,
                     start=args.start if args.start is not None else cfg.backtest.start,
                     end=args.end if args.end is not None else cfg.backtest.end)
        cfg = replace(cfg, backtest=bt)

    if args.interval is not None:
        cfg = replace(cfg, rebalance_interval=args.interval)
    if args.band is not None:
        cfg = replace(cfg, rebalance_band=args.band)
    if args.maker:
        cfg = replace(cfg, taker_fee_bps=cfg.maker_fee_bps, slippage_bps=0.0)

    if args.compare_dte is not None:
        dte_list = args.compare_dte if args.compare_dte else [0, 1]
        run_compare_dte(cfg, dte_list)
        return

    if args.dte is not None:
        cfg = replace(cfg, expiry_offset_days=args.dte)
        cfg.validate()
    run_backtest(cfg)


if __name__ == "__main__":
    main()
