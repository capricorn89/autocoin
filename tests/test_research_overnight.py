"""research.overnight 핵심 계산 (네트워크 없음)."""
import numpy as np
import pandas as pd
import pytest

from research.overnight import common as C
from src import binance_data


def test_gap_split_decomposes_close_to_close():
    idx = pd.bdate_range("2024-01-01", periods=4)
    o = pd.Series([100, 103, 99, 101.0], idx)
    c = pd.Series([102, 100, 100, 104.0], idx)
    g = C.gap_split(o, c)
    assert g["overnight"].iloc[0] == pytest.approx(103 / 102 - 1)
    assert g["intraday"].iloc[0] == pytest.approx(100 / 103 - 1)
    # (1+밤)(1+장중) = (1+보유)
    np.testing.assert_allclose((1 + g["overnight"]) * (1 + g["intraday"]), 1 + g["buy&hold"])


def test_div_adjusted_puts_dividend_into_overnight():
    # 전일 종가 100, 배당 2 → 배당락일 시가 98 원시가. 보정하면 오버나잇 0
    idx = pd.bdate_range("2024-01-01", periods=2)
    d = pd.DataFrame({"Open": [100, 98.0], "Close": [100, 98.0], "Adj Close": [98, 98.0]}, idx)
    a = C.div_adjusted(d)
    assert a["Open"].iloc[1] / a["Close"].iloc[0] - 1 == pytest.approx(0.0)


def test_perf_basic():
    r = pd.Series([0.01] * 252)
    p = C.perf(r)
    assert p["CAGR"] == pytest.approx(1.01 ** 252 - 1)
    assert p["MDD"] == 0 and p["승률"] == 1


def test_swings_labels_bear_from_peak_and_bull_from_trough():
    px = pd.Series([100, 110, 120, 100, 90, 95, 110, 115.0], pd.bdate_range("2024-01-01", periods=8))
    lab = C.swings(px, 0.20)
    # 120 → 90 이 -25% → 고점(120, i=2)부터 bear. 90 → 110 이 +22% → 저점(90, i=4)부터 bull
    assert list(lab) == ["bull", "bull", "bear", "bear", "bull", "bull", "bull", "bull"]


def test_night_windows_kst_and_dst():
    days = pd.DatetimeIndex(["2026-03-06", "2026-03-09"])   # 금 → 월, 미국 서머타임 3/8 시작
    k = C.night_windows(days, C.KRX_OPEN, C.KRX_CLOSE, C.KST).iloc[0]
    assert k["entry"] == pd.Timestamp("2026-03-06 06:30", tz="UTC")
    assert k["exit"] == pd.Timestamp("2026-03-09 00:00", tz="UTC")
    u = C.night_windows(days, C.US_OPEN, C.US_CLOSE, C.NY).iloc[0]
    assert u["entry"] == pd.Timestamp("2026-03-06 21:00", tz="UTC")   # EST 16:00
    assert u["exit"] == pd.Timestamp("2026-03-09 13:30", tz="UTC")    # EDT 09:30


def test_night_windows_offsets():
    days = pd.DatetimeIndex(["2026-09-17", "2026-09-18"])
    w = C.night_windows(days, C.KRX_OPEN, C.KRX_CLOSE, C.KST, exit_offset_min=-1).iloc[0]
    assert w["exit"] == pd.Timestamp("2026-09-17 23:59", tz="UTC")


def test_hold_returns_funding_strictly_inside_window():
    t = pd.date_range("2026-09-17 06:30", "2026-09-18 00:00", freq="1min", tz="UTC")
    px = pd.Series(np.linspace(100, 110, len(t)), t)
    w = pd.DataFrame({"entry": [t[0]], "exit": [t[-1]]}, index=[pd.Timestamp("2026-09-18")])
    fu = pd.Series([0.001, 0.002, 0.004],
                   pd.DatetimeIndex(["2026-09-17 08:00:00.001", "2026-09-17 16:00:00.001",
                                     "2026-09-18 00:00:00.001"], tz="UTC"))
    h = C.hold_returns(px, w, fu)
    assert h["ret"].iloc[0] == pytest.approx(0.10)
    assert h["funding"].iloc[0] == pytest.approx(0.003)   # 00:00 펀딩은 청산 이후라 제외


def test_hold_returns_drops_windows_without_prices():
    t = pd.date_range("2026-09-17 06:30", periods=3, freq="1min", tz="UTC")
    px = pd.Series([1.0, 2, 3], t)
    w = pd.DataFrame({"entry": [t[0], t[0]], "exit": [t[2], t[2] + pd.Timedelta(hours=1)]},
                     index=pd.DatetimeIndex(["2026-09-18", "2026-09-19"]))
    assert list(C.hold_returns(px, w).index) == [pd.Timestamp("2026-09-18")]


def test_fetch_funding_rates_paginates(monkeypatch):
    calls = []
    pages = [[{"fundingTime": i, "fundingRate": "0.0001", "markPrice": "100"} for i in range(1000)],
             [{"fundingTime": 1000 + i, "fundingRate": "-0.0002", "markPrice": "101"} for i in range(5)]]

    def fake(path, params):
        calls.append(params["startTime"])
        return pages[len(calls) - 1]

    monkeypatch.setattr(binance_data, "_request", fake)
    monkeypatch.setattr(binance_data._time, "sleep", lambda s: None)
    df = binance_data.fetch_funding_rates("EWYUSDT", 0, 10_000)
    assert calls == [0, 1000]
    assert len(df) == 1005 and df["fundingRate"].iloc[-1] == pytest.approx(-0.0002)
    assert str(df.index.tz) == "UTC"
