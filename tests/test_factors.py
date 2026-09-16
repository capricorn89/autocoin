"""팩터 계산 검증: 값, 수집중단·stale 구간 배제, 무체결 처리."""
from __future__ import annotations

import pandas as pd
import pytest

from src.factors import FACTORS, TARGET, build, corr_table

START = pd.Timestamp("2026-01-01 00:00:00", tz="UTC")
N = 300


def ts(i: int) -> pd.Timestamp:
    return START + pd.Timedelta(seconds=i)


def make_book(drop: range | None = None, bid_qty: float = 30.0, ask_qty: float = 10.0) -> pd.DataFrame:
    """1초마다 1행, mid = 100 + 0.01*i, 5호가 잔량 고정 → LobImbalance = 0.5."""
    rows = [{"ts": ts(i), "bid_px": 100.0 + 0.01 * i - 0.01, "ask_px": 100.0 + 0.01 * i + 0.01,
             "bid_qty": bid_qty, "ask_qty": ask_qty}
            for i in range(N) if drop is None or i not in drop]
    return pd.DataFrame(rows)


def make_trades(buy: float = 3.0, sell: float = 1.0) -> pd.DataFrame:
    """1초마다 매수주도 buy, 매도주도 sell → TxnImbalance = (3-1)/4 = 0.5."""
    rows = []
    for i in range(N):
        rows.append({"ts": ts(i), "qty": buy, "sell": False})
        rows.append({"ts": ts(i), "qty": sell, "sell": True})
    return pd.DataFrame(rows)


def test_factor_values() -> None:
    df = build(make_book(), make_trades(), lookback=60, horizon=10, grid=10)
    row = df.loc[ts(100)]
    assert row.TxnImbalance == pytest.approx(0.5)          # (180-60)/240
    assert row.LobImbalance == pytest.approx(0.5)          # (30-10)/40
    assert row.PastReturn == pytest.approx(101.0 / 100.4 - 1)   # mid[100]/mid[40]
    assert row[TARGET] == pytest.approx(101.1 / 101.0 - 1)      # mid[110]/mid[100]
    secs = (df.index - pd.Timestamp(0, tz="UTC")) // pd.Timedelta(seconds=1)
    assert (secs % 10 == 0).all()                           # 10초 격자


def test_outage_window_excluded() -> None:
    """[t-lookback, t+horizon] 이 수집중단과 겹치는 표본은 버린다."""
    outages = [(ts(120), ts(140))]
    df = build(make_book(), make_trades(), lookback=60, horizon=10, grid=10, outages=outages)
    assert ts(100) in df.index                              # 창 [40,110] — 겹치지 않음
    for i in range(110, 201, 10):                           # 창이 중단과 겹치는 구간
        assert ts(i) not in df.index
    assert ts(210) in df.index                              # 창 [150,220] — 복구 이후


def test_stale_book_excluded() -> None:
    """5호가가 max_stale 초 넘게 갱신되지 않으면 LOCF 값을 상관에 넣지 않는다."""
    df = build(make_book(drop=range(150, 201)), make_trades(),
               lookback=60, horizon=10, grid=10, max_stale=30.0)
    assert ts(160) in df.index                              # 창 [100,170] — stale 30초 이내
    for i in range(170, 261, 10):                           # stale > 30 인 초(180~200)를 포함
        assert ts(i) not in df.index
    assert ts(270) in df.index


def test_no_trades_drops_txn_imbalance() -> None:
    """무체결 창은 방향 정보가 없으므로 표본에서 빠진다."""
    assert build(make_book(), pd.DataFrame(columns=["ts", "qty", "sell"]),
                 lookback=60, horizon=10, grid=10).empty


def test_corr_table_rank_correlation() -> None:
    """scipy 없이도 Spearman 이 계산되는지 (단조 비선형에서 1.0)."""
    df = pd.DataFrame({"TxnImbalance": [1.0, 2.0, 3.0, 4.0, 5.0],
                       "LobImbalance": [5.0, 4.0, 3.0, 2.0, 1.0],
                       "PastReturn": [0.0, 1.0, 0.0, 1.0, 0.0],
                       TARGET: [1.0, 4.0, 9.0, 16.0, 25.0]})
    out = corr_table(df)
    assert out.loc["TxnImbalance", "spearman"] == pytest.approx(1.0)
    assert out.loc["LobImbalance", "spearman"] == pytest.approx(-1.0)
    assert out.loc["TxnImbalance", "pearson"] == pytest.approx(0.9811, abs=1e-4)
    assert list(out.index) == FACTORS and (out.n == 5).all()
