"""매일 일치 대조: 그날 DB(수집기)를 백테스트 경로(replay.run_day)로 재생한 엔진 판단 vs 실행 프로세스 엔진 판단(signals).

거래 단위로 (신호초 t_sig, 가정 진입가 p_in, 가정 청산가 p_out, 사유) 를 비교한다. 실제 체결가·슬리피지는 리포트에서 따로 본다.
실행 프로세스가 판단할 수 없던 구간(꺼져 있음, 시작·재시작 뒤 워밍업 662초)은 '미커버'로 빼고 일치율을 낸다.
다르면 원인 후보를 붙인다: 시작·재시작 직후, WS 끊김·갭(실행 프로세스), 수집기 쪽 갭(DB), 늦은 체결(WS 지연).

    python -m src.strategies.pastofi_crypto.reconcile --mode paper --date 2026-10-26 [--notify]
"""
from __future__ import annotations

import argparse
import json
import logging
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from .feed import DAY
from .replay import load_day, run_day
from .spec import Spec
from .store import read_jsonl

log = logging.getLogger(__name__)
REPO = Path(__file__).resolve().parents[3]
STATE_ROOT = REPO / "data" / "pastofi"
WARMUP = 662


def _epoch(d: date) -> int:
    return (d - date(1970, 1, 1)).days * DAY


def coverage(events: list[dict], day0: int, day1: int) -> list[tuple[float, float]]:
    """실행 프로세스가 판단할 수 있던 구간 [시작+662, 다음 시작 또는 종료]. events.jsonl 의 start/stop 으로 만든다."""
    marks = []
    for e in events:
        if e.get("event") in ("start", "stop"):
            t = datetime.fromisoformat(e["logged_at"]).timestamp()
            marks.append((t, e["event"]))
    marks.sort()
    out = []
    for i, (t, ev) in enumerate(marks):
        if ev != "start":
            continue
        end = marks[i + 1][0] if i + 1 < len(marks) else day1
        a, b = max(t + WARMUP, day0), min(end, day1)
        if b > a:
            out.append((a, b))
    return out


def _explain(sym: str, t: int, events: list[dict], db_gaps: list[dict]) -> list[str]:
    why = []
    for e in events:
        try:
            et = datetime.fromisoformat(e["logged_at"]).timestamp()
        except Exception:
            continue
        if not (t - WARMUP - 300 <= et <= t + 300):
            continue
        if e.get("event") == "start":
            why.append(f"실행 프로세스 시작 {datetime.fromtimestamp(et, timezone.utc):%H:%M:%S}")
        elif e.get("event") == "conn" and e.get("what") != "connected":
            why.append(f"WS {e.get('stream')} 끊김 {datetime.fromtimestamp(et, timezone.utc):%H:%M:%S} ({e.get('reason', '')[:60]})")
        elif e.get("event") == "gap" and e.get("symbol") == sym:
            why.append(f"실행 쪽 {e.get('stream')} 갭 {datetime.fromtimestamp(et, timezone.utc):%H:%M:%S}")
    for g in db_gaps:
        if t - WARMUP - 300 <= g["t"] <= t + 300:
            why.append(f"수집기(DB) {g['stream']} 갭/끊김 {datetime.fromtimestamp(g['t'], timezone.utc):%H:%M:%S}")
    return sorted(set(why)) or ["원인 미상 — WS 지연(늦은 체결·호가) 의심"]


def db_gaps(d: date, dsn: str | None = None) -> dict[str, list[dict]]:
    import psycopg
    from ...storage.db import get_dsn
    lo = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    out: dict[str, list] = {}
    with psycopg.connect(get_dsn(dsn)) as conn:
        for t, kind, sym, stream in conn.execute(
                "SELECT recv_ts, kind, symbol, stream FROM market.collector_events WHERE recv_ts >= %s AND recv_ts < %s "
                "AND (kind = 'gap' OR (kind = 'conn' AND detail->>'event' = 'disconnected'))",
                (lo - timedelta(minutes=20), lo + timedelta(days=1, minutes=10))).fetchall():
            out.setdefault(sym or "*", []).append({"t": t.timestamp(), "stream": stream or kind})
    return out


def reconcile(mode: str, d: date, state_root: Path = STATE_ROOT, spec: Spec | None = None) -> dict:
    spec = spec or Spec.load()
    sdir = state_root / mode
    day0, day1 = _epoch(d), _epoch(d) + DAY
    events = read_jsonl(sdir / "events.jsonl", ("logged_at", "event", "symbol", "stream", "what"))
    signals = [s for s in read_jsonl(sdir / "signals.jsonl", ("symbol", "t_sig")) if day0 <= s["t_sig"] < day1]
    trades = [t for t in read_jsonl(sdir / "trades.jsonl", ("trade_id",)) if day0 <= t["t_sig"] < day1]
    cov = coverage(events, day0, day1)
    covered = lambda t: any(a <= t < b for a, b in cov)
    try:
        gaps = db_gaps(d)
    except Exception as e:
        log.warning("수집기 이벤트 조회 실패: %s", e)
        gaps = {}
    res = {"mode": mode, "day": d.isoformat(), "coverage": cov, "symbols": {}, "computed_at": datetime.now(timezone.utc).isoformat()}
    for sym in spec.symbols:
        q, t = load_day(sym, d)
        exp = run_day(spec, sym, d, q, t) if len(q) >= 1000 else []
        exp_by = {day0 + int(r["t_sig"]): r for r in exp}
        live_by = {s["t_sig"]: s for s in signals if s["symbol"] == sym}
        rows, n_match = [], 0
        for ts in sorted(set(exp_by) | set(live_by)):
            e, l = exp_by.get(ts), live_by.get(ts)
            row = {"t_sig": ts, "time": datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S"),
                   "replay": e and {k: e[k] for k in ("p_in", "p_out", "reason", "pnl_bp")},
                   "live": l and {k: l.get(k) for k in ("p_in", "p_out", "reason", "engine_bp", "status", "skip_reason")}}
            if e and l:
                same = (abs(float(e["p_in"]) - float(l["p_in"] or 0)) < 1e-9 and l.get("p_out") is not None
                        and abs(float(e["p_out"]) - float(l["p_out"])) < 1e-9 and e["reason"] == l.get("reason"))
                row["result"] = "일치" if same else "값 다름"
            elif e:
                row["result"] = "실행 쪽 없음" if covered(ts) else "미커버"
            else:
                row["result"] = "실행 쪽만 있음"
            if row["result"] == "일치":
                n_match += 1
            elif row["result"] != "미커버":
                row["why"] = _explain(sym, ts, events, gaps.get(sym, []) + gaps.get("*", []))
            rows.append(row)
        n_rep = sum(1 for r in rows if r["replay"] and r["result"] != "미커버")
        n_live = sum(1 for r in rows if r["live"])
        res["symbols"][sym] = {"n_replay": n_rep, "n_live": n_live, "n_match": n_match, "rows": rows,
                               "n_uncovered": sum(1 for r in rows if r["result"] == "미커버")}
    tot = {k: sum(v[k] for v in res["symbols"].values()) for k in ("n_replay", "n_live", "n_match", "n_uncovered")}
    denom = max(tot["n_replay"], tot["n_live"])
    res["total"] = {**tot, "rate": (tot["n_match"] / denom) if denom else None,
                    "traded": len(trades), "net_usd": sum(t.get("net_usd") or 0 for t in trades)}
    res["ok"] = tot["n_match"] == tot["n_replay"] == tot["n_live"]
    return res


def save(res: dict, state_root: Path = STATE_ROOT) -> Path:
    d = state_root / res["mode"] / "reconcile"
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"{res['day']}.json"
    path.write_text(json.dumps(res, ensure_ascii=False, indent=1))
    try:
        import psycopg
        from ...storage.db import get_dsn
        from .store import ensure_schema
        ensure_schema()
        with psycopg.connect(get_dsn(None)) as conn:
            cov0 = datetime.fromtimestamp(res["coverage"][0][0], timezone.utc) if res["coverage"] else None
            for sym, v in res["symbols"].items():
                conn.execute(
                    "INSERT INTO exec.pastofi_reconcile (mode, day, symbol, n_replay, n_live, n_match, covered_from, detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (mode, day, symbol) DO UPDATE SET n_replay=EXCLUDED.n_replay, "
                    "n_live=EXCLUDED.n_live, n_match=EXCLUDED.n_match, covered_from=EXCLUDED.covered_from, "
                    "detail=EXCLUDED.detail, computed_at=now()",
                    (res["mode"], res["day"], sym, v["n_replay"], v["n_live"], v["n_match"], cov0,
                     json.dumps(v, ensure_ascii=False)))
    except Exception as e:
        log.warning("대조 결과 DB 저장 실패 (파일엔 남음): %s", e)
    return path


def text(res: dict) -> str:
    t = res["total"]
    rate = f"{t['rate'] * 100:.0f}%" if t["rate"] is not None else "-"
    head = ("✅" if res["ok"] else "⚠️") + f" 일치 대조 {res['day']} (UTC): 재생 {t['n_replay']}건 · 실행 {t['n_live']}건 · 일치 {t['n_match']}건 ({rate})"
    lines = [head]
    if t["n_uncovered"]:
        lines.append(f"미커버(꺼져 있음·워밍업) {t['n_uncovered']}건은 뺐습니다.")
    for sym, v in res["symbols"].items():
        for r in v["rows"]:
            if r["result"] not in ("일치", "미커버"):
                lines.append(f"· {sym} {r['time']} {r['result']}: {'; '.join(r.get('why', []))}")
    lines.append(f"그날 실제 거래 {t['traded']}건 · 순손익 {'+' if t['net_usd'] >= 0 else '−'}${abs(t['net_usd']):.4f}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.reconcile")
    ap.add_argument("--mode", choices=["paper", "live"], default="paper")
    ap.add_argument("--date", required=True)
    ap.add_argument("--notify", action="store_true")
    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    res = reconcile(a.mode, date.fromisoformat(a.date))
    path = save(res)
    msg = text(res)
    print(msg)
    print(path)
    if a.notify:
        from ...notify import TelegramNotifier
        tg = TelegramNotifier.from_env()
        if tg:
            tg.prefix = f"[pastofi {a.mode.upper()}]"
            if not tg.send(msg):
                tg.send(msg)


if __name__ == "__main__":
    main()
