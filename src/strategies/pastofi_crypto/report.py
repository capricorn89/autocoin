"""페이퍼(또는 라이브) 성과 리포트 — 7일 뒤 라이브 판단용.

거래 수, 수수료 전·후 손익, 승률, 종목별, 백테스트 재생과의 일치율, 가정가 대비 슬리피지, 날짜별 손익, 최대 손실 구간,
건너뛴 신호(사유별, 가상 결과).

    python -m src.strategies.pastofi_crypto.report --mode paper          # 출력 + results/ 에 마크다운 저장
"""
from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .spec import Spec
from .store import read_jsonl

REPO = Path(__file__).resolve().parents[3]
STATE_ROOT = REPO / "data" / "pastofi"


def _d(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _avg(xs):
    xs = [x for x in xs if x is not None]
    return sum(xs) / len(xs) if xs else None


def _f(x, nd=1, unit=""):
    return "-" if x is None else f"{x:+.{nd}f}{unit}"


def max_drawdown(trades: list[dict]) -> dict:
    """순손익 누적 곡선의 최대 낙폭 (청산 시각 순)."""
    cum = peak = 0.0
    peak_t = start = end = None
    mdd = 0.0
    for t in sorted(trades, key=lambda r: r.get("exit_ts") or 0):
        cum += t.get("net_usd") or 0.0
        if cum > peak or peak_t is None:
            peak, peak_t = max(cum, peak), t.get("exit_ts")
        dd = cum - peak
        if dd < mdd:
            mdd, start, end = dd, peak_t, t.get("exit_ts")
    return {"usd": mdd, "from": start, "to": end}


def build_report(mode: str, state_dir: Path | None = None) -> dict:
    d = state_dir or STATE_ROOT / mode
    spec = Spec.load()
    trades = sorted(read_jsonl(d / "trades.jsonl", ("trade_id",)), key=lambda r: r["t_sig"])
    signals = read_jsonl(d / "signals.jsonl", ("symbol", "t_sig"))
    state = json.loads((d / "state.json").read_text()) if (d / "state.json").exists() else {}
    recs = [json.loads(p.read_text()) for p in sorted((d / "reconcile").glob("*.json"))] if (d / "reconcile").exists() else []

    n = len(trades)
    gross = sum(t["gross_usd"] or 0 for t in trades)
    fees = sum(t["fees_usd"] or 0 for t in trades)
    net = sum(t["net_usd"] or 0 for t in trades)
    wins = sum(1 for t in trades if (t["net_usd"] or 0) > 0)
    by_sym = defaultdict(list)
    by_day = defaultdict(list)
    for t in trades:
        by_sym[t["symbol"]].append(t)
        by_day[_d(t["exit_ts"] or t["t_sig"])].append(t)
    reasons = defaultdict(int)
    for t in trades:
        reasons[t["reason"]] += 1
    skipped = [s for s in signals if s.get("status") == "skipped"]
    skip_by = defaultdict(list)
    for s in skipped:
        skip_by[(s.get("skip_reason") or "?").split(" (")[0]].append(s)
    rep_n = sum(r["total"]["n_replay"] for r in recs)
    live_n = sum(r["total"]["n_live"] for r in recs)
    match_n = sum(r["total"]["n_match"] for r in recs)
    mdd = max_drawdown(trades)
    out = {
        "mode": mode, "generated_at": datetime.now(timezone.utc).isoformat(), "spec": spec.version,
        "period": [state.get("started_at"), state.get("trial_end")],
        "n": n, "gross": gross, "fees": fees, "net": net, "win_rate": wins / n if n else None,
        "avg_pnl_bp": _avg([t["pnl_bp"] for t in trades]), "avg_assumed_bp": _avg([t["assumed_bp"] for t in trades]),
        "avg_slip_in_bp": _avg([t["slip_in_bp"] for t in trades]), "avg_slip_out_bp": _avg([t["slip_out_bp"] for t in trades]),
        "reasons": dict(reasons), "mdd": mdd,
        "by_symbol": {s: {"n": len(v), "net": sum(t["net_usd"] or 0 for t in v), "gross": sum(t["gross_usd"] or 0 for t in v),
                          "avg_bp": _avg([t["pnl_bp"] for t in v])} for s, v in sorted(by_sym.items())},
        "by_day": {k: {"n": len(v), "net": sum(t["net_usd"] or 0 for t in v)} for k, v in sorted(by_day.items())},
        "reconcile": {"days": len(recs), "replay": rep_n, "live": live_n, "match": match_n,
                      "rate": match_n / max(rep_n, live_n) if max(rep_n, live_n) else None,
                      "by_day": {r["day"]: r["total"] for r in recs}},
        "skipped": {k: {"n": len(v), "virtual_bp": sum((s.get("engine_bp") or 0) for s in v)} for k, v in skip_by.items()},
        "cum_state": state.get("cum_net"), "halted": state.get("halted"),
    }
    out["text"] = summary_text(out)
    out["markdown"] = markdown(out, trades)
    return out


def summary_text(r: dict) -> str:
    wr = f"{r['win_rate'] * 100:.0f}%" if r["win_rate"] is not None else "-"
    rc = r["reconcile"]
    rate = f"{rc['rate'] * 100:.0f}%" if rc["rate"] is not None else "-"
    lines = [f"거래 {r['n']}건 · 승률 {wr} · 수수료 전 ${r['gross']:+.4f} · 수수료 ${r['fees']:.4f} · 수수료 후 ${r['net']:+.4f}",
             f"평균 {_f(r['avg_pnl_bp'])}bp (가정가 기준 {_f(r['avg_assumed_bp'])}bp) · 슬리피지 진입 {_f(r['avg_slip_in_bp'], 2)}bp / 청산 {_f(r['avg_slip_out_bp'], 2)}bp",
             f"최대 손실 구간 ${r['mdd']['usd']:+.4f} · 사유 {r['reasons']}",
             f"백테스트 재생 일치 {rc['match']}/{max(rc['replay'], rc['live'])} ({rate}, {rc['days']}일)",
             "종목별 " + " · ".join(f"{s[:3]} {v['n']}건 ${v['net']:+.4f}" for s, v in r["by_symbol"].items())]
    if r["skipped"]:
        lines.append("건너뜀 " + " · ".join(f"{k} {v['n']}건(가상 {v['virtual_bp']:+.0f}bp)" for k, v in r["skipped"].items()))
    return "\n".join(lines)


def markdown(r: dict, trades: list[dict]) -> str:
    p0, p1 = r["period"]
    per = f"{_d(p0)} ~ {_d(p1)}" if p0 else "-"
    L = [f"# pastofi 코인판 {r['mode']} 리포트 ({r['spec']})", "", f"- 기간(페이퍼 7일 기준): {per} UTC",
         f"- 생성: {r['generated_at']}", "", "## 요약", "", "```", r["text"], "```", "",
         "## 종목별", "", "| 종목 | 건 | 수수료 전 $ | 수수료 후 $ | 평균 bp |", "|---|---:|---:|---:|---:|"]
    for s, v in r["by_symbol"].items():
        L.append(f"| {s} | {v['n']} | {v['gross']:+.4f} | {v['net']:+.4f} | {_f(v['avg_bp'])} |")
    L += ["", "## 날짜별 (UTC, 청산일)", "", "| 날짜 | 건 | 순손익 $ | 대조 재생/실행/일치 |", "|---|---:|---:|---|"]
    days = sorted(set(r["by_day"]) | set(r["reconcile"]["by_day"]))
    for k in days:
        v = r["by_day"].get(k, {"n": 0, "net": 0.0})
        rc = r["reconcile"]["by_day"].get(k)
        L.append(f"| {k} | {v['n']} | {v['net']:+.4f} | " + (f"{rc['n_replay']}/{rc['n_live']}/{rc['n_match']}" if rc else "-") + " |")
    L += ["", "## 거래", "", "| 신호(UTC) | 종목 | 진입 | 청산 | 사유 | bp | 가정 bp | 순 $ | 수수료 $ | 슬리피지 진입/청산 bp |",
          "|---|---|---:|---:|---|---:|---:|---:|---:|---|"]
    for t in trades:
        L.append(f"| {datetime.fromtimestamp(t['t_sig'], timezone.utc):%m/%d %H:%M:%S} | {t['symbol']} | {t['entry_px']} | "
                 f"{t['exit_px']} | {t['reason']} | {_f(t['pnl_bp'])} | {_f(t['assumed_bp'])} | {t['net_usd']:+.4f} | "
                 f"{t['fees_usd']:.4f} | {_f(t['slip_in_bp'], 2)} / {_f(t['slip_out_bp'], 2)} |")
    if r["skipped"]:
        L += ["", "## 건너뛴 신호 (엔진은 계속 돌아 가상 결과가 있다)", ""]
        for k, v in r["skipped"].items():
            L.append(f"- {k}: {v['n']}건, 가상 합 {v['virtual_bp']:+.1f}bp (비용 전)")
    return "\n".join(L) + "\n"


def write_report(r: dict, state_dir: Path | None = None) -> Path:
    out = REPO / "results" / f"pastofi_{r['mode']}_{datetime.now(timezone.utc):%Y%m%d}.md"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(r["markdown"], encoding="utf-8")
    d = (state_dir or STATE_ROOT / r["mode"])
    (d / "report.json").write_text(json.dumps({k: v for k, v in r.items() if k != "markdown"}, ensure_ascii=False, default=str))
    return out


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.report")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    a = ap.parse_args(argv)
    r = build_report(a.mode)
    print(r["text"])
    print(write_report(r))


if __name__ == "__main__":
    main()
