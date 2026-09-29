"""국내 KOSPI200 ETF 로 밤 보유: KODEX 200·TIGER 200, 연·월 분해, SPY 밤과 50/50 복합.

    python -m research.overnight.kodex

체결 가정: 15:30 종가 동시호가 매수 → 다음 거래일 09:00 시가 동시호가 매도.
동시호가라 스프레드 비용은 없고, ETF 매도는 거래세 면제라 비용은 증권사 수수료뿐이다.
분배금 과세(15.4%)·유휴 현금 이자·SPY 배당 원천징수·해외 양도세는 반영하지 않았다.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from . import common as C

FEE_KODEX = 2 * 1.5 / 1e4    # 편도 1.5bp (일반 온라인 수수료) 왕복
FEE_SPY = 2 * 0.5 / 1e4      # 편도 0.5bp (미국 증권사 MOC/MOO 가정) 왕복


def etf_table() -> tuple[pd.DataFrame, dict[str, pd.DataFrame]]:
    E = {"KODEX 200": C.etf_returns("069500"), "TIGER 200": C.etf_returns("102110")}
    rows = []
    for nm, df in E.items():
        for c in ("overnight", "intraday", "buy&hold"):
            rows.append({"ETF": nm, "구간": c, "비용": "-", **C.perf(df[c])})
        for bp in (0.5, 1.5):
            rows.append({"ETF": nm, "구간": "overnight", "비용": f"편도 {bp}bp",
                         **C.perf(df["overnight"] - 2 * bp / 1e4)})
    return pd.DataFrame(rows), E


def regime_table(K: pd.DataFrame, lab: pd.Series) -> pd.DataFrame:
    rows = []
    for s in ("bull", "bear"):
        for nm, r in (("밤 수수료0", K["overnight"]), ("밤 1.5bp", K["overnight"] - FEE_KODEX),
                      ("장중", K["intraday"]), ("계속 보유", K["buy&hold"])):
            l = lab.reindex(r.index, method="ffill")
            rows.append({"국면": s, "전략": nm, **C.perf(r[l == s])})
    return pd.DataFrame(rows)


def detail(K: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """밤(1.5bp) vs 계속 보유: 연도별 지표, 월별 수익, 달력월 평균."""
    night, hold, day = K["overnight"] - FEE_KODEX, K["buy&hold"], K["intraday"]

    def yst(r):
        eq = (1 + r).cumprod()
        return pd.Series({"수익": eq.iloc[-1] - 1, "변동성": r.std() * np.sqrt(252),
                          "샤프": r.mean() / r.std() * np.sqrt(252), "MDD": (eq / eq.cummax() - 1).min()})

    g = night.index.year
    Y = pd.concat({"밤": night.groupby(g).apply(yst).unstack(),
                   "보유": hold.groupby(g).apply(yst).unstack()}, axis=1)
    Y[("차이", "수익")] = Y[("밤", "수익")] - Y[("보유", "수익")]
    Y[("장중", "수익")] = C.compound(day, g)
    mp = night.index.to_period("M")
    M = pd.DataFrame({"밤": C.compound(night, mp), "보유": C.compound(hold, mp), "장중": C.compound(day, mp)})
    M["차이"] = M["밤"] - M["보유"]
    cal = M.groupby(M.index.month).agg(밤=("밤", "mean"), 보유=("보유", "mean"), 장중=("장중", "mean"),
                                        밤우위비율=("차이", lambda x: (x > 0).mean()))
    up, dn = M[M["보유"] > 0], M[M["보유"] <= 0]
    capture = {"상승월 캡처": up["밤"].mean() / up["보유"].mean(), "하락월 캡처": dn["밤"].mean() / dn["보유"].mean()}
    return {"yearly": Y, "monthly": M, "calendar": cal, "capture": capture}


def combo(K: pd.DataFrame) -> dict:
    """KODEX 밤 + SPY 밤, 두 계좌 50/50 일별 재조정(현지통화). 휴장일 수익 0.

    두 창이 겹치므로(한국 15:30~22:30·05:00~09:00) 같은 돈을 순차로 쓸 수 없어 자금을 나눈다.
    """
    spy = C.div_adjusted(C.load_yf("SPY"))
    s_on = (spy["Open"] / spy["Close"].shift() - 1).loc["2010-01-01":]
    s_bh = spy["Close"].pct_change().loc["2010-01-01":]
    end = min(K.index[-1], s_on.index[-1])
    idx = K.index.union(s_on.index)
    idx = idx[(idx >= "2010-01-04") & (idx <= end)]
    years = (idx[-1] - idx[0]).days / 365.25

    def st(r):   # 합집합 달력이라 연수는 달력일로 환산
        eq = (1 + r).cumprod()
        return {"CAGR": eq.iloc[-1] ** (1 / years) - 1, "변동성": r.std() * np.sqrt(252),
                "샤프": r.mean() / r.std() * np.sqrt(252), "MDD": (eq / eq.cummax() - 1).min()}

    rows = []
    for kf, sf in ((0, 0), (0.5, 0.5), (1.5, 0.5), (1.5, 1), (1.5, 3)):
        rk = (K["overnight"] - 2 * kf / 1e4).reindex(idx).fillna(0)
        rs = (s_on - 2 * sf / 1e4).reindex(idx).fillna(0)
        rows.append({"KODEX bp": kf, "SPY bp": sf, "구성": "50/50", **st(0.5 * rk + 0.5 * rs)})
        rows.append({"KODEX bp": kf, "SPY bp": sf, "구성": "KODEX 밤", **st(rk)})
        rows.append({"KODEX bp": kf, "SPY bp": sf, "구성": "SPY 밤", **st(rs)})
    bh = 0.5 * K["buy&hold"].reindex(idx).fillna(0) + 0.5 * s_bh.reindex(idx).fillna(0)
    rows.append({"KODEX bp": np.nan, "SPY bp": np.nan, "구성": "50/50 계속 보유", **st(bh)})
    rk = (K["overnight"] - FEE_KODEX).reindex(idx).fillna(0)
    rs = (s_on - FEE_SPY).reindex(idx).fillna(0)
    return {"table": pd.DataFrame(rows), "corr": rk.corr(rs), "series": 0.5 * rk + 0.5 * rs, "hold": bh}


def plot(K: pd.DataFrame, D: dict, lab: pd.Series) -> None:
    plt = C.setup_plot()
    from matplotlib.colors import TwoSlopeNorm
    fig, ax = plt.subplots(figsize=(16, 7))
    blocks = (lab != lab.shift()).cumsum()
    for _, g in lab.groupby(blocks):
        if g.iloc[0] == "bear":
            ax.axvspan(g.index[0], g.index[-1], color="#d9534f", alpha=.12, lw=0)
    ax.plot((1 + K["overnight"]).cumprod(), color="#1f5fbf", lw=.9, alpha=.5, label="밤 보유 (수수료 0)")
    ax.plot((1 + K["overnight"] - FEE_KODEX).cumprod(), color="#1f5fbf", lw=2, label="밤 보유 (편도 1.5bp)")
    ax.plot((1 + K["intraday"]).cumprod(), color="#d9534f", lw=1.2, label="장중 보유")
    ax.plot((1 + K["buy&hold"]).cumprod(), color="#333", lw=1.2, label="계속 보유 (분배금 재투자)")
    ax.set_yscale("log"); ax.grid(alpha=.3); ax.legend(loc="upper left"); ax.axhline(1, color="k", lw=.6)
    ax.set_title("KODEX 200 종가 매수 → 다음 날 시가 매도. 붉은 음영 = KOSPI200 하락장")
    fig.tight_layout(); fig.savefig(C.out_path("kodex_night.png"), dpi=130); plt.close(fig)

    M = D["monthly"]
    piv = lambda s: s.groupby([s.index.year, s.index.month]).first().unstack()
    fig, axes = plt.subplots(1, 3, figsize=(22, 9))
    for ax, (col, t) in zip(axes, (("밤", "밤 보유 (편도 1.5bp)"), ("보유", "계속 보유"), ("차이", "차이 (밤 - 보유), %p"))):
        P = piv(M[col]); v = P.values * 100; lim = np.nanpercentile(np.abs(v), 95)
        ax.imshow(v, cmap="RdBu", norm=TwoSlopeNorm(0, -lim, lim), aspect="auto")
        ax.set_xticks(range(12), [f"{m}월" for m in range(1, 13)]); ax.set_yticks(range(len(P)), P.index)
        for i in range(v.shape[0]):
            for j in range(v.shape[1]):
                if not np.isnan(v[i, j]):
                    ax.text(j, i, f"{v[i, j]:.1f}", ha="center", va="center", fontsize=7.5,
                            color="white" if abs(v[i, j]) > lim * .6 else "black")
        ax.set_title(t)
    fig.suptitle("KODEX 200 월별 수익률 (%)", fontsize=14)
    fig.tight_layout(); fig.savefig(C.out_path("kodex_monthly_heatmap.png"), dpi=120); plt.close(fig)


def main() -> dict:
    T, E = etf_table(); T.to_csv(C.out_path("kodex_stats.csv"), index=False)
    K = E["KODEX 200"]
    lab = C.swings(C.load_ks200()["Close"])
    RG = regime_table(K, lab); RG.to_csv(C.out_path("kodex_regime.csv"), index=False)
    D = detail(K)
    D["yearly"].to_csv(C.out_path("kodex_yearly.csv")); D["monthly"].to_csv(C.out_path("kodex_monthly.csv"))
    D["calendar"].to_csv(C.out_path("kodex_calendar.csv"))
    CB = combo(K); CB["table"].to_csv(C.out_path("combo_stats.csv"), index=False)
    plot(K, D, lab)
    with pd.option_context("display.width", 220, "display.float_format", "{:,.3f}".format):
        print(T.to_string(index=False)); print(RG.to_string(index=False))
        print(D["yearly"].to_string()); print(D["calendar"].to_string()); print(D["capture"])
        print(CB["table"].to_string(index=False)); print("KODEX 밤 vs SPY 밤 상관:", round(CB["corr"], 3))
    k15 = T[(T["ETF"] == "KODEX 200") & (T["비용"] == "편도 1.5bp")].iloc[0]
    cb = CB["table"]
    base = cb[(cb["KODEX bp"] == 1.5) & (cb["SPY bp"] == 0.5) & (cb["구성"] == "50/50")].iloc[0]
    return {"KODEX 밤 1.5bp CAGR": k15["CAGR"], "KODEX 밤 1.5bp MDD": k15["MDD"],
            "50/50 복합 CAGR": base["CAGR"], "50/50 복합 샤프": base["샤프"]}


if __name__ == "__main__":
    main()
