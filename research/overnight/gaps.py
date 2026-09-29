"""지수 수준 오버나잇(종가→시가) vs 장중(시가→종가): KOSPI200·SPY·EWY, 2010~.

    python -m research.overnight.gaps
"""
from __future__ import annotations

import pandas as pd

from . import common as C


def returns() -> dict[str, pd.DataFrame]:
    ks = C.load_ks200()
    return {"KOSPI200": C.gap_split(ks["Open"], ks["Close"]),
            "SPY": _split(C.div_adjusted(C.load_yf("SPY"))),
            "EWY": _split(C.div_adjusted(C.load_yf("EWY")))}


def _split(d: pd.DataFrame) -> pd.DataFrame:
    d = d[d.index >= "2009-12-31"]
    return C.gap_split(d["Open"], d["Close"])


def stats_table(R: dict[str, pd.DataFrame]) -> pd.DataFrame:
    rows = []
    for k, df in R.items():
        for c in ("overnight", "intraday", "buy&hold"):
            rows.append({"시장": k, "구간": c, **C.perf(df[c])})
        for bp in (1, 3, 5):
            rows.append({"시장": k, "구간": f"overnight 편도 {bp}bp",
                         **C.perf(df["overnight"] - 2 * bp / 1e4)})
    return pd.DataFrame(rows)


def yearly(R: dict[str, pd.DataFrame]) -> pd.DataFrame:
    return pd.DataFrame({f"{k} {c}": C.compound(df[c], df.index.year)
                         for k, df in R.items() for c in ("overnight", "intraday")})


def plot(R: dict[str, pd.DataFrame]) -> None:
    plt = C.setup_plot()
    colors = {"overnight": "#1f5fbf", "intraday": "#d9534f", "buy&hold": "#555555"}
    fig, axes = plt.subplots(1, 3, figsize=(18, 5.5))
    for ax, (k, df) in zip(axes, R.items()):
        for c in df:
            ax.plot((1 + df[c]).cumprod(), label=c, color=colors[c], lw=1.4)
        ax.set_yscale("log"); ax.set_title(f"{k} (2010~)"); ax.grid(alpha=.3); ax.legend()
        ax.axhline(1, color="k", lw=.6)
    fig.suptitle("오버나잇(종가→시가) vs 장중(시가→종가) 누적수익, 비용 전, 로그축")
    fig.tight_layout(); fig.savefig(C.out_path("gaps_cumulative.png"), dpi=130); plt.close(fig)


def main() -> dict:
    R = returns()
    T = stats_table(R); T.to_csv(C.out_path("gaps_stats.csv"), index=False)
    Y = yearly(R); Y.to_csv(C.out_path("gaps_yearly.csv"))
    plot(R)
    with pd.option_context("display.width", 200, "display.float_format", "{:,.3f}".format):
        print(T.to_string(index=False)); print(Y.to_string())
    ks = T[(T["시장"] == "KOSPI200") & (T["구간"] == "overnight")].iloc[0]
    spy = T[(T["시장"] == "SPY") & (T["구간"] == "overnight")].iloc[0]
    return {"KOSPI200 오버나잇 CAGR": ks["CAGR"], "SPY 오버나잇 CAGR": spy["CAGR"]}


if __name__ == "__main__":
    main()
