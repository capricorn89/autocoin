"""라이브/페이퍼 실행 엔진.

백테스트와 동일한 StraddleReplicator 를 사용해 실시간 가격으로 매매한다.
 - 기본은 페이퍼(PaperBroker): 실제 주문 없이 시뮬 체결 + 로그.
 - execution.mode='live' + API 키가 있을 때만 BinanceBroker 사용(실주문은 기본 비활성).
 - 상태(포지션/현금/사이클)를 JSON 으로 영속화하여 재시작에 대응.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import time
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from src import binance_data as bd
from src.broker import PaperBroker
from src.config import Config, load_config
from .strategy import StraddleReplicator

_RUNNING = True


def _load_env(path=".env"):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        os.environ.setdefault(k.strip(), v.strip())


def _current_sigma(cfg: Config) -> float:
    if cfg.vol_mode == "fixed":
        return cfg.vol_value
    daily = bd.load_klines(cfg.symbol, "1d", use_cache=False)
    vs = bd.realized_vol_series(daily["close"], cfg.vol_window).dropna()
    if len(vs):
        return float(vs.iloc[-1])
    return cfg.vol_value


def _save_state(path: str, broker: PaperBroker, eng: StraddleReplicator):
    state = {
        "position": broker.position, "cash": broker.cash,
        "fees_paid": broker.fees_paid, "trades": len(broker.trades),
        "K": eng.K,
        "anchor": eng.anchor.isoformat() if eng.anchor else None,
        "expiry": eng.expiry.isoformat() if eng.expiry else None,
        "updated": datetime.now(timezone.utc).isoformat(),
    }
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_text(json.dumps(state, indent=2))


def _resume_or_seed(cfg: Config, broker, eng: StraddleReplicator):
    """중도 시작/재시작 시 현재 활성 사이클을 복원.

    1) 상태파일의 anchor 가 현재 사이클과 일치하면 포지션/현금까지 재개(PaperBroker).
    2) 아니면 진입 시각(예: 08:00) 가격을 klines 에서 받아 K 만 복원(포지션 0에서 합류).
    """
    now = datetime.now(timezone.utc)
    win = eng.active_window(now)
    if win is None:
        return  # flat 구간 — 다음 진입 때 자연히 시작
    anchor, expiry = win

    p = Path(cfg.execution.state_file)
    if isinstance(broker, PaperBroker) and p.exists():
        try:
            s = json.loads(p.read_text())
            if s.get("anchor") == anchor.isoformat() and s.get("K") is not None:
                broker.position = float(s.get("position", 0.0))
                broker.cash = float(s.get("cash", 0.0))
                broker.fees_paid = float(s.get("fees_paid", 0.0))
                eng.seed(anchor, expiry, float(s["K"]))
                print(f"상태 재개: pos={broker.position:+.2f} K={s['K']:.2f}")
                return
        except Exception as e:
            print(f"상태파일 무시({e})")

    kp = bd.get_price_at(cfg.symbol, int(anchor.timestamp() * 1000))
    if kp:
        eng.seed(anchor, expiry, kp)
        print(f"활성 사이클 복원: K({cfg.strike_time})={kp:.2f} — 현 포지션에서 합류")


def _stop(*_):
    global _RUNNING
    _RUNNING = False
    print("\n중지 신호 수신 — 종료합니다.")


def run_live(cfg: Config, paper: bool = True, once: bool = False):
    _load_env()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    eng = StraddleReplicator(cfg)

    if paper or cfg.execution.mode == "paper":
        broker = PaperBroker(taker_fee_bps=cfg.taker_fee_bps, slippage_bps=cfg.slippage_bps)
        mode = "PAPER(시뮬)"
    else:
        from .broker import BinanceBroker
        broker = BinanceBroker(cfg.symbol, os.getenv("BINANCE_API_KEY"),
                               os.getenv("BINANCE_API_SECRET"))
        mode = "LIVE(실주문)"

    print(f"=== 라이브 엔진 시작 [{mode}] {cfg.symbol} ===")
    print(f"진입 {cfg.strike_time} → 만기 {cfg.expiry_time}(+{cfg.expiry_offset_days}d) "
          f"{cfg.strike_timezone} | 리밸런스: {cfg.rebalance_interval} | "
          f"폴링: {cfg.execution.poll_seconds}s")

    _resume_or_seed(cfg, broker, eng)

    sigma = _current_sigma(cfg)
    last_sigma_update = time.time()

    while _RUNNING:
        try:
            ts = datetime.now(timezone.utc)
            price = bd.get_mark_price(cfg.symbol)

            # 변동성은 하루 1회만 갱신(분 단위 폴링마다 재계산 불필요)
            if time.time() - last_sigma_update > 3600:
                sigma = _current_sigma(cfg)
                last_sigma_update = time.time()

            dec = eng.step(ts, price, sigma)
            if dec.new_cycle:
                print(f"\n[{ts:%Y-%m-%d %H:%M:%S}Z] 진입 K={dec.K:.2f} "
                      f"(만기까지 {dec.tau*365*24:.1f}h, σ={sigma:.3f})")
            if dec.settled:
                print(f"[{ts:%Y-%m-%d %H:%M:%S}Z] 만기 정산 prev_K={dec.prev_K:.2f} "
                      f"S_T={price:.2f} payoff={abs(price-dec.prev_K):.2f} → 청산")

            if dec.is_rebalance:
                tr = broker.rebalance_to(ts, dec.target_position, price,
                                         band=cfg.rebalance_band,
                                         reason="reset" if dec.new_cycle else
                                                ("settle" if dec.settled else "rebal"))
                eq = broker.equity(price)
                kstr = f"{dec.K:.2f}" if dec.K is not None else "----"
                tag = "ACTIVE" if dec.active else "FLAT"
                line = (f"[{ts:%H:%M:%S}Z] {tag} px={price:.2f} K={kstr} "
                        f"δ={dec.target_position:+.2f} pos={broker.position:+.2f} "
                        f"eq={eq:+.2f}")
                if tr:
                    line += f"  TRADE {tr.qty:+.2f}@{tr.price:.2f} fee={tr.fee:.4f}"
                print(line)
                _save_state(cfg.execution.state_file, broker, eng)
            elif once:
                # --once 인데 매매 시점이 아니어도 현재 상태를 보여줌
                tag = "ACTIVE" if dec.active else "FLAT"
                kstr = f"{dec.K:.2f}" if dec.K is not None else "----"
                print(f"[{ts:%H:%M:%S}Z] {tag} px={price:.2f} K={kstr} "
                      f"δ={dec.target_position:+.2f} pos={broker.position:+.2f}")

            if once:
                break
            time.sleep(cfg.execution.poll_seconds)
        except Exception as e:  # 폴링 오류는 치명적이지 않게 재시도
            print(f"  오류: {e} — {cfg.execution.poll_seconds}s 후 재시도")
            if once:
                raise
            time.sleep(cfg.execution.poll_seconds)

    print("엔진 종료.")


def main():
    ap = argparse.ArgumentParser(description="EWYUSDT 스트래들 복제 라이브 엔진")
    ap.add_argument("--config", default="legacy/straddle/config.yaml")
    ap.add_argument("--paper", action="store_true", help="강제 페이퍼 모드(기본)")
    ap.add_argument("--live", action="store_true", help="실주문 모드(API 키 필요, opt-in)")
    ap.add_argument("--once", action="store_true", help="1틱만 실행 후 종료(검증용)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    paper = not args.live
    run_live(cfg, paper=paper, once=args.once)


if __name__ == "__main__":
    main()
