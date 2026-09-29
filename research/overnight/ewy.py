"""EWYUSDT 시간대별 보유 + 상장 전 EWY 대체 + 국면 분해 + 라이브 리스크.

    python -m research.overnight.ewy

한국 장 기준 밤 = KRX 15:30 → 다음 KRX 거래일 09:00 (KST).
미국 장 기준 밤 = NYSE 16:00 → 다음 NYSE 거래일 09:30 (ET, 서머타임 반영).

상장 전 대체값 (EWY 는 한국 밤 시작·끝 시각에 거래되지 않는다):
    한국 밤(d) ≈ log(EWY 종가 as of 09:00 d+1) - log(EWY 종가 as of 09:00 d) - log(1 + KOSPI200 장중 d)
EWY 종가→종가에서 그 사이 KRX 장중을 빼서 밤 부분만 남긴다. 미국 휴장일에는 인접 밤끼리
수익이 넘어가지만 누적하면 상쇄된다. 겹치는 기간 실제와 상관 0.72, 누적은 보수적(71% vs 137%).
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import common as C

FEE_TAKER = 2 * 5 / 1e4     # 편도 5bp 왕복
FEE_MAKER = 2 * 2 / 1e4     # 편도 2bp 왕복


# ---------------------------------------------------------------- 1. 실제 EWYUSDT
def perp_windows() -> dict[tuple[str, str], pd.DataFrame]:
    px = C.load_perp_1m()["open"]
    fu = C.load_funding()
    kd = C.krx_days()
    spy = C.load_yf("SPY"); ud = spy.index[spy.index >= "2009-12-31"]
    return {
        ("KRX", "overnight"): C.hold_returns(px, C.night_windows(kd, C.KRX_OPEN, C.KRX_CLOSE, C.KST), fu),
        ("KRX", "intraday"): C.hold_returns(px, C.day_windows(kd, C.KRX_OPEN, C.KRX_CLOSE, C.KST), fu),
        ("US", "overnight"): C.hold_returns(px, C.night_windows(ud, C.US_OPEN, C.US_CLOSE, C.NY), fu),
        ("US", "intraday"): C.hold_returns(px, C.day_windows(ud, C.US_OPEN, C.US_CLOSE, C.NY), fu),
    }


def perp_table(P: dict) -> pd.DataFrame:
    rows = []
    for (base, kind), df in P.items():
        for fee, lab in ((0, "수수료0"), (FEE_MAKER, "maker 2bp"), (FEE_TAKER, "taker 5bp")):
            rows.append({"기준": base, "구간": kind, "비용": lab, "n": len(df),
                         "펀딩합": df["funding"].sum(),
                         **C.perf(df["ret"] - df["funding"] - fee)})
    px = C.load_perp_1m()["open"]
    bh = px.resample("1D").first().pct_change().dropna()
    rows.append({"기준": "24/7", "구간": "buy&hold", "비용": "펀딩 제외", "n": len(bh), **C.perf(bh, 365)})
    return pd.DataFrame(rows)


# ---------------------------------------------------------------- 2. 상장 전 대체
def proxy_nights() -> pd.DataFrame:
    """KRX 거래일 d 의 밤을 d+1(청산일) 인덱스로. proxy(USD)·ks_night(KRW)·ks_id."""
    ks = C.load_ks200()
    ks_id = np.log(ks["Close"] / ks["Open"])
    e = C.load_yf("EWY")
    close_t = pd.Series(np.log(e["Adj Close"].to_numpy()),
                        index=[C.session_ts(d, C.US_CLOSE, C.NY) for d in e.index])
    opens = pd.DatetimeIndex([C.session_ts(d, C.KRX_OPEN, C.KST) for d in ks.index])
    pos = close_t.index.searchsorted(opens, side="left") - 1   # 09:00 KST 직전 마지막 EWY 종가
    A = np.where(pos >= 0, close_t.to_numpy()[np.clip(pos, 0, None)], np.nan)
    resid = A[1:] - A[:-1] - ks_id.to_numpy()[:-1]
    return pd.DataFrame({"proxy": np.expm1(resid),
                         "ks_night": ks["Open"].to_numpy()[1:] / ks["Close"].to_numpy()[:-1] - 1,
                         "ks_id": np.expm1(ks_id.to_numpy()[:-1])}, index=ks.index[1:])


def spliced(P: dict, proxy: pd.DataFrame) -> pd.DataFrame:
    """상장 전 EWY 대체값 + 상장 후 EWYUSDT 실제. gross 와 funding(상장 전 0 가정)."""
    act = P[("KRX", "overnight")]
    cut = act.index[0]
    pre = proxy[proxy.index < cut]
    return pd.concat([pd.DataFrame({"gross": pre["proxy"], "funding": 0.0, "src": "EWY 대체"}),
                      pd.DataFrame({"gross": act["ret"], "funding": act["funding"], "src": "EWYUSDT"})]
                     ).sort_index()


def validation(P: dict, proxy: pd.DataFrame) -> dict:
    j = P[("KRX", "overnight")].join(proxy, how="inner")
    return {"n": len(j), "상관(대체, 실제)": j["ret"].corr(j["proxy"]),
            "상관(KOSPI200 오버나잇, 실제)": j["ret"].corr(j["ks_night"]),
            "누적 실제": (1 + j["ret"]).prod() - 1, "누적 대체": (1 + j["proxy"]).prod() - 1}


def regime_tables(N: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.Series]:
    ks = C.load_ks200()["Close"]
    lab = C.swings(ks)
    l = lab.reindex(N.index, method="ffill")
    rows = []
    for fee, nm in ((0, "수수료0"), (FEE_MAKER, "maker 2bp"), (FEE_TAKER, "taker 5bp")):
        r = N["gross"] - N["funding"] - fee
        for s in ("전체", "bull", "bear"):
            rows.append({"수수료": nm, "국면": s, **C.perf(r if s == "전체" else r[l == s])})
    blocks = (lab != lab.shift()).cumsum()
    segs = []
    for _, g in lab.groupby(blocks):
        t0, t1 = g.index[0], g.index[-1]
        sel = (N.index > t0) & (N.index <= t1)
        segs.append({"구간": f"{t0.date()}~{t1.date()}", "국면": g.iloc[0],
                     "KOSPI200": ks[t1] / ks[t0] - 1,
                     "밤_비용전": (1 + N["gross"][sel]).prod() - 1,
                     "밤_taker": (1 + (N["gross"] - N["funding"] - FEE_TAKER)[sel]).prod() - 1,
                     "밤_maker": (1 + (N["gross"] - N["funding"] - FEE_MAKER)[sel]).prod() - 1,
                     "일수": int(sel.sum())})
    return pd.DataFrame(rows), pd.DataFrame(segs), lab


# ---------------------------------------------------------------- 3. 라이브 리스크
def live_risk() -> dict:
    m = C.load_perp_1m()
    px = m["open"]
    fu = C.load_funding()
    kd = C.krx_days()
    kd = kd[kd >= "2026-03-16"]

    def nights(entry_off=0, exit_off=0):
        w = C.night_windows(kd, C.KRX_OPEN, C.KRX_CLOSE, C.KST, exit_off, entry_off)
        return C.hold_returns(px, w, fu).join(w)

    base = nights()
    mae = [m.loc[a:b - pd.Timedelta(minutes=1), "low"].min() / px[a] - 1
           for a, b in zip(base["entry"], base["exit"])]
    base["mae"] = mae
    base["days"] = (base["d1"] - base["d0"]).dt.days
    net = base["ret"] - base["funding"] - FEE_TAKER
    losing = (base["ret"] < 0).astype(int)
    r20 = (1 + net).rolling(20).apply(np.prod, raw=True) - 1
    sens = [{"구분": "진입", "분": o, "bp/밤": nights(o, 0)["ret"].mean() * 1e4} for o in (-10, -5, 0, 5, 10, 30)]
    sens += [{"구분": "청산", "분": o, "bp/밤": nights(0, o)["ret"].mean() * 1e4} for o in (-30, -10, -1, 0, 1, 5, 10)]
    f0 = fu[fu.index.hour == 0]
    return {
        "n": len(base), "평균": base["ret"].mean(), "std": base["ret"].std(),
        "t": base["ret"].mean() / base["ret"].std() * np.sqrt(len(base)),
        "최악 밤": base["ret"].min(), "최대역행": min(mae),
        "역행<-5% 밤": int((base["mae"] < -0.05).sum()), "역행<-8% 밤": int((base["mae"] < -0.08).sum()),
        "최장 연속손실": int(losing.groupby((losing != losing.shift()).cumsum()).sum().max()),
        "MDD(taker+펀딩)": ((1 + net).cumprod() / (1 + net).cumprod().cummax() - 1).min(),
        "20밤 손실확률": (r20.dropna() < 0).mean(), "20밤 최악": r20.min(),
        "00시 펀딩 최대": f0.max(), "00시 펀딩 |>5bp| 비율": (f0.abs() > 5e-4).mean(),
        "시각 민감도": pd.DataFrame(sens),
    }


# ---------------------------------------------------------------- 출력
def plot(P: dict, N: pd.DataFrame, lab: pd.Series) -> None:
    plt = C.setup_plot()
    fig, ax = plt.subplots(figsize=(14, 5.5))
    for (base, kind), df in P.items():
        col = "#1f5fbf" if base == "KRX" else "#e0a020"
        ax.plot((1 + df["ret"] - df["funding"] - FEE_TAKER).cumprod(), color=col,
                ls="-" if kind == "overnight" else "--", lw=1.6, label=f"{base} {kind} (taker+펀딩)")
    ax.set_title("EWYUSDT 시간대별 보유"); ax.grid(alpha=.3); ax.legend(fontsize=8); ax.axhline(1, color="k", lw=.6)
    fig.tight_layout(); fig.savefig(C.out_path("ewy_perp_windows.png"), dpi=130); plt.close(fig)

    ks = C.load_ks200()["Close"]
    fig, ax = plt.subplots(2, 1, figsize=(16, 9), sharex=True, gridspec_kw={"height_ratios": [3, 2]})
    blocks = (lab != lab.shift()).cumsum()
    for _, g in lab.groupby(blocks):
        if g.iloc[0] == "bear":
            for a in ax:
                a.axvspan(g.index[0], g.index[-1], color="#d9534f", alpha=.12, lw=0)
    for fee, lab_, col, lw in ((0, "비용 전", "#8fb0e0", .9), (FEE_MAKER, "maker 편도 2bp", "#8e44ad", 1.4),
                               (FEE_TAKER, "taker 편도 5bp", "#1f5fbf", 1.8)):
        ax[0].plot((1 + N["gross"] - N["funding"] - fee).cumprod(), color=col, lw=lw, label=f"한국 장 기준 밤 ({lab_})")
    ewy = C.load_yf("EWY")["Adj Close"]; ewy = ewy[ewy.index >= "2009-12-31"]
    ax[0].plot(ewy / ewy.iloc[0], color="#6aa84f", lw=1.2, label="EWY 계속 보유")
    ax[0].axvline(N[N["src"] == "EWYUSDT"].index[0], color="k", ls=":", lw=1)
    ax[0].set_yscale("log"); ax[0].grid(alpha=.3); ax[0].legend(loc="upper left")
    ax[0].set_title("EWY(상장 전)·EWYUSDT(상장 후) 한국 장 기준 밤 보유, 붉은 음영 = KOSPI200 하락장")
    ax[1].plot(ks, color="#333", lw=1); ax[1].set_yscale("log"); ax[1].set_title("KOSPI200"); ax[1].grid(alpha=.3)
    fig.tight_layout(); fig.savefig(C.out_path("ewy_night_regimes.png"), dpi=130); plt.close(fig)


def main() -> dict:
    P = perp_windows()
    T = perp_table(P); T.to_csv(C.out_path("ewy_perp_stats.csv"), index=False)
    proxy = proxy_nights(); proxy.to_csv(C.out_path("ewy_proxy_nights.csv"))
    V = validation(P, proxy)
    N = spliced(P, proxy); N.to_csv(C.out_path("ewy_night_spliced.csv"))
    SC, SEG, lab = regime_tables(N)
    SC.to_csv(C.out_path("ewy_regime_fee.csv"), index=False); SEG.to_csv(C.out_path("ewy_regime_segments.csv"), index=False)
    lab.to_csv(C.out_path("regime_labels.csv"))
    L = live_risk(); L["시각 민감도"].to_csv(C.out_path("ewy_timing_sensitivity.csv"), index=False)
    plot(P, N, lab)
    with pd.option_context("display.width", 220, "display.float_format", "{:,.4f}".format):
        print(T.to_string(index=False)); print(V); print(SC.to_string(index=False)); print(SEG.to_string(index=False))
        print({k: v for k, v in L.items() if k != "시각 민감도"}); print(L["시각 민감도"].to_string(index=False))
    krx_on = T[(T["기준"] == "KRX") & (T["구간"] == "overnight")].set_index("비용")
    sens = L["시각 민감도"].set_index(["구분", "분"])["bp/밤"]
    return {"EWYUSDT 한국 밤 총수익(비용0)": krx_on.loc["수수료0", "총수익"],
            "EWYUSDT 한국 밤 총수익(taker)": krx_on.loc["taker 5bp", "총수익"],
            "대체값 상관": V["상관(대체, 실제)"],
            "연결 CAGR(비용0)": SC[(SC["수수료"] == "수수료0") & (SC["국면"] == "전체")]["CAGR"].iloc[0],
            "연결 CAGR(taker)": SC[(SC["수수료"] == "taker 5bp") & (SC["국면"] == "전체")]["CAGR"].iloc[0],
            "청산 정시 bp/밤": sens[("청산", 0)], "청산 +1분 bp/밤": sens[("청산", 1)],
            "최대역행": L["최대역행"]}


if __name__ == "__main__":
    main()
