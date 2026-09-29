"""오버나잇 효과 연구 공용: 데이터 로드(캐시 우선)·성과 지표·세션 시각.

데이터는 data/overnight/ 에 CSV 로 캐시한다. 캐시가 있으면 네트워크를 타지 않으므로
같은 캐시로는 항상 같은 숫자가 나온다. 캐시를 지우면 원천에서 다시 받는다
(yfinance 수정주가·배당 이력은 사후에 바뀔 수 있어 숫자가 달라질 수 있다).

가정 (2026-09-29 분석 기준, WOO-95)
 - KOSPI200 지수 시가·종가: FinanceDataReader 'KS200' (KRX 원천). yfinance ^KS200 은 시가가 0 이라 못 쓴다
 - SPY·EWY: yfinance 원시 OHLC + (Adj Close / Close) 배당 보정을 시가·종가에 같이 적용
 - KODEX 200·TIGER 200: yfinance 원시가 + 분배금(분배락일 시가에 더함). FDR 은 2014-07 이후 수정주가만 있어 안 쓴다
 - EWYUSDT: 바이낸스 1분봉 시가 = 그 분 시작 시점 가격
"""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

REPO_ROOT = Path(__file__).resolve().parents[2]
DATA_DIR = REPO_ROOT / "data" / "overnight"
OUT_DIR = REPO_ROOT / "results" / "overnight"

START = "2010-01-01"
END = "2026-09-28"          # O0 스펙 동결 시점의 데이터 종료일 (재현 기준)
PERP = "EWYUSDT"
PERP_ONBOARD_MS = 1773667800000   # 2026-03-16 13:30 UTC

KST = "Asia/Seoul"
NY = "America/New_York"
KRX_OPEN, KRX_CLOSE = "09:00", "15:30"
US_OPEN, US_CLOSE = "09:30", "16:00"

YF_CODES = {"SPY": "SPY", "EWY": "EWY", "069500": "069500.KS", "102110": "102110.KS",
            "USDKRW": "KRW=X"}


# ---------------------------------------------------------------- 로드
def _cache(name: str) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    return DATA_DIR / f"{name}.csv"


def load_ks200() -> pd.DataFrame:
    path = _cache("KS200")
    if not path.exists():
        import FinanceDataReader as fdr
        fdr.DataReader("KS200", "2009-12-01").to_csv(path)
    d = pd.read_csv(path, index_col=0, parse_dates=True)[["Open", "Close"]]
    return d[(d.index >= START) & (d.index <= END)]


def load_yf(name: str) -> pd.DataFrame:
    """원시 OHLC + Adj Close. name 은 YF_CODES 키."""
    path = _cache(name)
    if not path.exists():
        import yfinance as yf
        d = yf.download(YF_CODES[name], start="2009-12-01", auto_adjust=False, progress=False)
        d.columns = [c[0] for c in d.columns]
        d.to_csv(path)
    d = pd.read_csv(path, index_col=0, parse_dates=True)
    return d[d.index <= END]


def load_dividends(name: str) -> pd.Series:
    """분배락일(KST 자정 기준 날짜) -> 1좌당 분배금(원)."""
    path = _cache(f"{name}_div")
    if not path.exists():
        import yfinance as yf
        yf.Ticker(YF_CODES[name]).dividends.to_csv(path)
    dv = pd.read_csv(path, index_col=0)
    idx = pd.to_datetime(dv.index, utc=True).tz_convert(KST).tz_localize(None).normalize()
    return pd.Series(dv.iloc[:, 0].values, index=idx, name="div")


def load_perp_1m() -> pd.DataFrame:
    path = _cache(f"{PERP}_1m")
    if not path.exists():
        from src.binance_data import fetch_klines
        end_ms = int((pd.Timestamp(END, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
        fetch_klines(PERP, "1m", PERP_ONBOARD_MS, end_ms).to_csv(path)
    return _read_ts(path)


def _read_ts(path: Path) -> pd.DataFrame:
    d = pd.read_csv(path, index_col="ts")
    d.index = pd.to_datetime(d.index, utc=True, format="ISO8601")   # 밀리초 유무가 섞여 있다
    return d


def load_funding() -> pd.Series:
    path = _cache(f"{PERP}_funding")
    if not path.exists():
        from src.binance_data import fetch_funding_rates
        end_ms = int((pd.Timestamp(END, tz="UTC") + pd.Timedelta(days=1)).timestamp() * 1000)
        fetch_funding_rates(PERP, PERP_ONBOARD_MS, end_ms).to_csv(path)
    return _read_ts(path)["fundingRate"].astype(float)


def krx_days() -> pd.DatetimeIndex:
    """KRX 거래일. 지수 데이터(FDR)가 늦게 갱신되므로 KODEX 200 거래일을 쓴다."""
    return load_yf("069500").index


# ---------------------------------------------------------------- 수익률
def gap_split(open_: pd.Series, close: pd.Series) -> pd.DataFrame:
    """오버나잇(전일 종가→시가) / 장중(시가→종가) / 보유(종가→종가)."""
    return pd.DataFrame({"overnight": open_ / close.shift() - 1,
                         "intraday": close / open_ - 1,
                         "buy&hold": close.pct_change()}).dropna()


def div_adjusted(d: pd.DataFrame) -> pd.DataFrame:
    """yfinance 원시가에 배당 보정 계수(Adj Close/Close)를 시가·종가에 동일 적용."""
    f = d["Adj Close"] / d["Close"]
    return pd.DataFrame({"Open": d["Open"] * f, "Close": d["Adj Close"]})


def etf_returns(name: str, tax: float = 0.0) -> pd.DataFrame:
    """국내 ETF 원시가 + 분배금. 밤 보유자는 분배락 전날 종가에 보유하므로 분배금을 받는다."""
    d = load_yf(name)[["Open", "Close"]]
    d = d[(d.index >= "2009-12-30") & (d["Open"] > 0)]
    div = load_dividends(name).reindex(d.index).fillna(0) * (1 - tax)
    out = pd.DataFrame({"overnight": (d["Open"] + div) / d["Close"].shift() - 1,
                        "intraday": d["Close"] / d["Open"] - 1,
                        "buy&hold": (d["Close"] + div) / d["Close"].shift() - 1,
                        "div": div})
    return out.iloc[1:]


# ---------------------------------------------------------------- 지표
def perf(r: pd.Series, ppy: float = 252) -> dict:
    """총수익·CAGR(관측 수/ppy 로 연수 환산)·변동성·샤프·MDD·승률."""
    r = r.dropna()
    if r.empty:
        return {}
    eq = (1 + r).cumprod()
    years = len(r) / ppy
    return {"총수익": eq.iloc[-1] - 1, "CAGR": eq.iloc[-1] ** (1 / years) - 1,
            "변동성": r.std() * np.sqrt(ppy),
            "샤프": r.mean() / r.std() * np.sqrt(ppy) if r.std() > 0 else np.nan,
            "MDD": (eq / eq.cummax() - 1).min(), "승률": (r > 0).mean()}


def compound(r: pd.Series, by) -> pd.Series:
    return (1 + r).groupby(by).prod() - 1


def swings(px: pd.Series, threshold: float = 0.20) -> pd.Series:
    """고점 대비 -threshold 면 그 고점부터 bear, 저점 대비 +threshold 면 그 저점부터 bull.

    사후 라벨이다 (전환점을 결과를 보고 정한다). 국면별 성과 분해에만 쓴다.
    """
    state, ext, ext_i = "bull", px.iloc[0], 0
    turns = [(0, "bull")]
    for i, p in enumerate(px.to_numpy()):
        if state == "bull":
            if p > ext:
                ext, ext_i = p, i
            elif p <= ext * (1 - threshold):
                turns.append((ext_i, "bear")); state, ext, ext_i = "bear", p, i
        else:
            if p < ext:
                ext, ext_i = p, i
            elif p >= ext * (1 + threshold):
                turns.append((ext_i, "bull")); state, ext, ext_i = "bull", p, i
    lab = np.empty(len(px), dtype=object)
    for k, (i0, s) in enumerate(turns):
        i1 = turns[k + 1][0] if k + 1 < len(turns) else len(px)
        lab[i0:i1] = s
    return pd.Series(lab, index=px.index, name="regime")


# ---------------------------------------------------------------- 세션 시각
def session_ts(day: pd.Timestamp, hhmm: str, tz: str) -> pd.Timestamp:
    """현지 날짜+시각 -> UTC (서머타임 반영)."""
    return pd.Timestamp(f"{day.date()} {hhmm}", tz=tz).tz_convert("UTC")


def night_windows(days: pd.DatetimeIndex, open_hhmm: str, close_hhmm: str, tz: str,
                  exit_offset_min: int = 0, entry_offset_min: int = 0) -> pd.DataFrame:
    """연속 거래일 (d0, d1) 마다 d0 장마감 → d1 장시작 창. 인덱스는 d1(청산일)."""
    rows = [{"d0": d0, "d1": d1,
             "entry": session_ts(d0, close_hhmm, tz) + pd.Timedelta(minutes=entry_offset_min),
             "exit": session_ts(d1, open_hhmm, tz) + pd.Timedelta(minutes=exit_offset_min)}
            for d0, d1 in zip(days[:-1], days[1:])]
    return pd.DataFrame(rows).set_index("d1", drop=False)


def day_windows(days: pd.DatetimeIndex, open_hhmm: str, close_hhmm: str, tz: str) -> pd.DataFrame:
    rows = [{"d1": d, "entry": session_ts(d, open_hhmm, tz), "exit": session_ts(d, close_hhmm, tz)}
            for d in days]
    return pd.DataFrame(rows).set_index("d1", drop=False)


def hold_returns(px: pd.Series, windows: pd.DataFrame, funding: pd.Series | None = None
                 ) -> pd.DataFrame:
    """창별 (exit/entry - 1) 과 창 안(entry < t < exit) 펀딩 합. 가격이 없는 창은 버린다."""
    ok = windows["entry"].isin(px.index) & windows["exit"].isin(px.index)
    w = windows[ok]
    out = pd.DataFrame({"ret": px.loc[w["exit"]].to_numpy() / px.loc[w["entry"]].to_numpy() - 1},
                       index=w.index)
    if funding is not None:
        out["funding"] = [funding[(funding.index > a) & (funding.index < b)].sum()
                          for a, b in zip(w["entry"], w["exit"])]
    else:
        out["funding"] = 0.0
    return out


# ---------------------------------------------------------------- 출력
def setup_plot():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from matplotlib import font_manager
    installed = {f.name for f in font_manager.fontManager.ttflist}
    for cand in ("AppleGothic", "Apple SD Gothic Neo", "NanumGothic", "Noto Sans CJK KR"):
        if cand in installed:
            plt.rcParams["font.family"] = cand
            break
    plt.rcParams["axes.unicode_minus"] = False
    return plt


def out_path(name: str) -> Path:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUT_DIR / name
