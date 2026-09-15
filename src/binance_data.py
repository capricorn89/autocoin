"""바이낸스 USDT-M 선물 과거/실시간 데이터 수집.

 - klines 페이지네이션 수집 후 data/ 에 CSV 캐시
 - 데이터 갭(휴장/주말) 탐지
 - 실현변동성 추정용 일봉 종가 헬퍼
 - 라이브용 최신가 조회
"""
from __future__ import annotations

import time as _time
from pathlib import Path

import pandas as pd
import requests

FAPI = "https://fapi.binance.com"
_INTERVAL_MS = {
    "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000,
    "30m": 1_800_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
}
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

_KLINE_COLS = [
    "open_time", "open", "high", "low", "close", "volume",
    "close_time", "qav", "trades", "tbav", "tbqv", "ignore",
]


def _request(path: str, params: dict) -> list:
    for attempt in range(5):
        try:
            r = requests.get(FAPI + path, params=params, timeout=20)
            if r.status_code == 429:
                _time.sleep(2 * (attempt + 1))
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException:
            if attempt == 4:
                raise
            _time.sleep(1.5 * (attempt + 1))
    return []


def get_onboard_date(symbol: str) -> int | None:
    """심볼 상장(onboard) 타임스탬프(ms)."""
    info = _request("/fapi/v1/exchangeInfo", {})
    for s in info.get("symbols", []):
        if s["symbol"] == symbol:
            return s.get("onboardDate")
    return None


def fetch_klines(symbol: str, interval: str, start_ms: int, end_ms: int) -> pd.DataFrame:
    """[start_ms, end_ms] 구간 klines 를 페이지네이션으로 모두 수집."""
    if interval not in _INTERVAL_MS:
        raise ValueError(f"지원하지 않는 interval: {interval}")
    step = _INTERVAL_MS[interval]
    rows: list[list] = []
    cur = start_ms
    while cur <= end_ms:
        batch = _request(
            "/fapi/v1/klines",
            {"symbol": symbol, "interval": interval, "startTime": cur,
             "endTime": end_ms, "limit": 1500},
        )
        if not batch:
            break
        rows.extend(batch)
        last_open = batch[-1][0]
        nxt = last_open + step
        if nxt <= cur:  # 진행 없음 -> 종료
            break
        cur = nxt
        if len(batch) < 1500:
            break
        _time.sleep(0.25)  # rate-limit 여유

    if not rows:
        return pd.DataFrame(columns=_KLINE_COLS)
    df = pd.DataFrame(rows, columns=_KLINE_COLS)
    df = df.drop_duplicates(subset="open_time").sort_values("open_time")
    for c in ("open", "high", "low", "close", "volume"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["ts"] = pd.to_datetime(df["open_time"], unit="ms", utc=True)
    return df.set_index("ts")[["open", "high", "low", "close", "volume"]]


def load_klines(symbol: str, interval: str, start_ms: int | None = None,
                end_ms: int | None = None, use_cache: bool = True) -> pd.DataFrame:
    """캐시 우선 로드. 캐시가 요청 구간을 못 덮으면 다시 받아 갱신."""
    DATA_DIR.mkdir(exist_ok=True)
    cache = DATA_DIR / f"{symbol}_{interval}.csv"

    if start_ms is None:
        ob = get_onboard_date(symbol)
        start_ms = ob if ob else 0
    if end_ms is None:
        end_ms = int(_time.time() * 1000)

    if use_cache and cache.exists():
        cached = pd.read_csv(cache, index_col="ts", parse_dates=["ts"])
        if not cached.empty:
            c_start = int(cached.index[0].timestamp() * 1000)
            c_end = int(cached.index[-1].timestamp() * 1000)
            # 캐시가 요청 구간을 충분히 덮으면 그대로 사용 (한 interval 여유)
            if c_start <= start_ms and c_end >= end_ms - _INTERVAL_MS[interval]:
                m = (cached.index >= pd.to_datetime(start_ms, unit="ms", utc=True)) & (
                    cached.index <= pd.to_datetime(end_ms, unit="ms", utc=True))
                return cached.loc[m]

    df = fetch_klines(symbol, interval, start_ms, end_ms)
    if use_cache and not df.empty:
        df.to_csv(cache)
    return df


def find_gaps(df: pd.DataFrame, interval: str) -> pd.DataFrame:
    """기대 간격보다 큰 시간 갭(휴장/주말 등)을 반환."""
    step = pd.Timedelta(milliseconds=_INTERVAL_MS[interval])
    diffs = df.index.to_series().diff()
    gaps = diffs[diffs > step * 1.5]
    return pd.DataFrame({"gap_start": gaps.index - gaps.values, "gap_end": gaps.index,
                         "gap": gaps.values})


def realized_vol_series(daily_close: pd.Series, window: int) -> pd.Series:
    """일봉 종가 -> 연율 실현변동성(roll std of log returns * sqrt(365)).

    각 날짜의 값은 '그 날까지' 정보로 계산되며, 사용 시 lookahead 방지를 위해
    호출측에서 직전 값을 참조해야 한다.
    """
    import numpy as np
    close = daily_close.astype(float)
    logret = np.log(close / close.shift(1))
    return logret.rolling(window).std() * (365 ** 0.5)


def get_price_at(symbol: str, ts_ms: int, interval: str = "1m") -> float | None:
    """특정 시각(ms)의 가격(해당 봉 시가). 갭이면 직전 유효 봉 시가."""
    step = _INTERVAL_MS[interval]
    batch = _request("/fapi/v1/klines",
                     {"symbol": symbol, "interval": interval,
                      "startTime": ts_ms - step * 5, "endTime": ts_ms + step, "limit": 10})
    if not batch:
        return None
    # open_time <= ts_ms 인 마지막 봉의 시가
    valid = [k for k in batch if k[0] <= ts_ms]
    chosen = valid[-1] if valid else batch[0]
    return float(chosen[1])


def get_mark_price(symbol: str) -> float:
    """라이브용 최신 가격(마크가)."""
    data = _request("/fapi/v1/premiumIndex", {"symbol": symbol})
    if isinstance(data, dict) and "markPrice" in data:
        return float(data["markPrice"])
    data = _request("/fapi/v1/ticker/price", {"symbol": symbol})
    return float(data["price"])
