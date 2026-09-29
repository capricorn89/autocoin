"""체결 로그 적재 + 판정 지표 리포트 (WOO-99).

    python -m src.overnight.report --mode live                 # 적재 + 지표 재계산 + 요약
    python -m src.overnight.report --mode live --telegram      # + 요약 텔레그램
    python -m src.overnight.report --compare                   # 같은 밤 페이퍼 vs 라이브

원본은 스케줄러의 data/overnight_live/<mode>/events.jsonl. DB(exec 스키마)에 (mode, 줄번호)로
멱등 적재하고, 밤별 지표를 계산해 exec.night_metrics 에 덮어쓴다.

지표 정의 (bp, "비용이면 +")
 - 기준가 = 바이낸스 1분봉 시가. 구간 시작 시각 기준 (진입 15:30, 청산 A 08:59 / B 08:57)
 - slip_entry = 진입 VWAP / 기준가 - 1, slip_exit = 1 - 청산 VWAP / 기준가
 - fee = 왕복 수수료 / 진입 명목, cost_rt = slip_entry + slip_exit + fee
 - gross = 청산 VWAP / 진입 VWAP - 1 (비용 전), bt_gross = 개장 시가 / 마감 시가 - 1 (백테스트 정의)
 - tracking = gross - bt_gross. 청산을 개장 1~3분 전에 하는 차이와 슬리피지가 함께 들어간다
 - 판정 (스펙 judgement): 정시 비율, A 시장가 슬리피지 평균, 추적오차 표준편차, B 왕복 비용 평균
"""
from __future__ import annotations

import argparse
import json
import statistics
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import psycopg

from ..storage.db import get_dsn
from .spec import SPEC_PATH, Spec

REPO = Path(__file__).resolve().parents[2]
STATE_ROOT = REPO / "data" / "overnight_live"
EXIT_LEGS = {"x", "f", "r", "s"}


def spec_for(version: int | None) -> Spec:
    return Spec.load(SPEC_PATH.with_name(f"spec_v{version or 1}.yaml"))


def leg_of(client_id: str | None) -> tuple[str | None, str | None]:
    """'on-20261013-e0' → ('2026-10-13', 'e')."""
    if not client_id or not client_id.startswith("on-"):
        return None, None
    parts = client_id.split("-")
    d = parts[1]
    return f"{d[:4]}-{d[4:6]}-{d[6:]}", parts[2][0]


# ---------------------------------------------------------------- 적재
def read_events(path: Path) -> list[tuple[int, dict]]:
    if not path.exists():
        return []
    out = []
    for i, line in enumerate(path.read_text(encoding="utf-8").splitlines()):
        if line.strip():
            out.append((i, json.loads(line)))
    return out


def ingest(mode: str, conn: psycopg.Connection, state_root: Path = STATE_ROOT) -> int:
    """events.jsonl → exec.events/fills/decisions/nights. 새로 들어간 줄 수."""
    rows = read_events(state_root / mode / "events.jsonl")
    done = {r[0] for r in conn.execute("SELECT line_no FROM exec.events WHERE mode = %s", (mode,)).fetchall()}
    new = 0
    for no, e in rows:
        if no in done:
            continue
        ed, leg = leg_of(e.get("client_id"))
        ed = e.get("entry_date") or ed
        conn.execute("INSERT INTO exec.events (mode, line_no, logged_at, event, client_id, entry_date, detail) "
                     "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                     (mode, no, e["logged_at"], e["event"], e.get("client_id"), ed, json.dumps(e, ensure_ascii=False)))
        if e["event"] == "filled" and "side" in e:
            conn.execute("INSERT INTO exec.fills (mode, client_id, ts, entry_date, leg, side, qty, price, fee, liquidity, line_no) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
                         (mode, e["client_id"], e["ts"], ed, leg, e["side"], e["qty"], e["price"], e["fee"],
                          e["liquidity"], no))
        elif e["event"] == "decision":
            conn.execute("INSERT INTO exec.decisions (mode, entry_date, leg, method, scheduled_at, decided_at, bid, ask, book_ts) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (mode, entry_date, leg) DO UPDATE SET "
                         "method=EXCLUDED.method, scheduled_at=EXCLUDED.scheduled_at, decided_at=EXCLUDED.decided_at, "
                         "bid=EXCLUDED.bid, ask=EXCLUDED.ask, book_ts=EXCLUDED.book_ts",
                         (mode, e["entry_date"], e["leg"], e["method"], e["scheduled_at"], e.get("decided_at"),
                          e.get("bid"), e.get("ask"), e.get("book_ts")))
        elif e["event"] == "night":
            conn.execute("INSERT INTO exec.nights (mode, entry_date, exit_date, method, phase, spec_version, qty, "
                         "entry_price, exit_price, stop_price, fees, funding, pnl, started_at, ended_at, notes) "
                         "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (mode, entry_date) DO UPDATE SET "
                         "phase=EXCLUDED.phase, qty=EXCLUDED.qty, entry_price=EXCLUDED.entry_price, exit_price=EXCLUDED.exit_price, "
                         "fees=EXCLUDED.fees, funding=EXCLUDED.funding, pnl=EXCLUDED.pnl, ended_at=EXCLUDED.ended_at, notes=EXCLUDED.notes",
                         (mode, e["entry_date"], e.get("exit_date"), e.get("method"), e["phase"], e.get("spec_version"),
                          e.get("qty"), e.get("entry_price"), e.get("exit_price"), e.get("stop_price"), e.get("fees"),
                          e.get("funding"), e.get("pnl"), e.get("started_at"), e.get("ended_at"),
                          json.dumps(e.get("notes", []), ensure_ascii=False)))
        new += 1
    return new


# ---------------------------------------------------------------- 지표 (순수 함수)
def _vwap(fills: list[dict]) -> float | None:
    q = sum(f["qty"] for f in fills)
    return sum(f["qty"] * f["price"] for f in fills) / q if q else None


def night_metrics(night: dict, fills: list[dict], decisions: dict[str, dict], spec: Spec,
                  ref: Callable[[datetime], float | None]) -> dict:
    """밤 하나의 판정 지표. fills 는 그 밤 체결(leg 포함), decisions 는 {leg: 호가 기록}."""
    ed = date.fromisoformat(str(night["entry_date"]))
    n = spec.night_for_entry(ed)
    method = night["method"]
    t = spec.order_times(n if n.method == method else _with_method(n, method))
    ent = [f for f in fills if f["leg"] == "e"]
    ext = [f for f in fills if f["leg"] in EXIT_LEGS]
    how = "손절" if any(f["leg"] == "s" for f in ext) else "청산"
    ve, vx = _vwap(ent), _vwap(ext)
    ref_e = ref(t.get("entry_limit", t["entry_market"]))
    ref_x = ref(t.get("exit_limit", t["exit_market"]))
    bt_e, bt_x = ref(n.entry_close), ref(n.exit_open)
    bp = lambda a, b: (a / b - 1) * 1e4 if a and b else None
    mid = lambda leg: ((decisions[leg]["bid"] + decisions[leg]["ask"]) / 2
                       if leg in decisions and decisions[leg].get("bid") else None)
    slip_e = bp(ve, ref_e)
    slip_x = -bp(vx, ref_x) if how == "청산" and bp(vx, ref_x) is not None else None
    qty_e = sum(f["qty"] for f in ent)
    fee_bp = sum(f["fee"] for f in fills) / (qty_e * ve) * 1e4 if qty_e else None
    gross, bt_gross = bp(vx, ve), bp(bt_x, bt_e)
    maker = lambda fs: sum(f["qty"] for f in fs if f["liquidity"] == "MAKER") / sum(f["qty"] for f in fs) if fs else None
    first_e = min((f["ts"] for f in ent), default=None)
    last_x = max((f["ts"] for f in ext), default=None)
    on_time = bool(first_e and first_e <= t["entry_market"] + timedelta(seconds=10)
                   and (how == "손절" or (last_x and last_x <= t["exit_flat_deadline"])))
    return {
        "entry_date": ed, "method": method, "how": how,
        "ref_entry": ref_e, "ref_exit": ref_x, "bt_entry": bt_e, "bt_exit": bt_x,
        "slip_entry_bp": slip_e, "slip_exit_bp": slip_x,
        "slip_entry_mid_bp": bp(ve, mid("e")), "slip_exit_mid_bp": -bp(vx, mid("x")) if how == "청산" and mid("x") else None,
        "fee_bp": fee_bp,
        "cost_rt_bp": (slip_e + slip_x + fee_bp) if None not in (slip_e, slip_x, fee_bp) else None,
        "maker_entry": maker(ent), "maker_exit": maker(ext),
        "gross_bp": gross, "bt_gross_bp": bt_gross,
        "tracking_bp": gross - bt_gross if None not in (gross, bt_gross) else None,
        "on_time": on_time, "pnl": night.get("pnl"),
    }


def _with_method(n, method):
    from dataclasses import replace
    return replace(n, method=method)


def summarize(rows: list[dict], spec: Spec, risk: dict | None = None) -> dict:
    j = spec.raw["judgement"]
    done = [r for r in rows if r.get("pnl") is not None]
    exits = [r for r in done if r["how"] == "청산"]
    a_slips = [v for r in exits if r["method"] == "A" for v in (r["slip_entry_bp"], r["slip_exit_bp"]) if v is not None]
    te = [r["tracking_bp"] for r in exits if r["tracking_bp"] is not None]
    b_cost = [r["cost_rt_bp"] for r in exits if r["method"] == "B" and r["cost_rt_bp"] is not None]
    b_maker = [v for r in exits if r["method"] == "B" for v in (r["maker_entry"], r["maker_exit"]) if v is not None]
    mean = lambda xs: statistics.fmean(xs) if xs else None
    std = lambda xs: statistics.stdev(xs) if len(xs) >= 2 else None
    on_time = mean([1.0 if r["on_time"] else 0.0 for r in done])
    checks = {
        "정시 비율": (on_time, j["on_time_ratio"], lambda v, lim: v >= lim),
        "A 슬리피지 평균 bp": (mean(a_slips), j["market_slippage_bps_max"], lambda v, lim: v <= lim),
        "추적오차 std bp": (std(te), j["tracking_error_std_bps_max"], lambda v, lim: v <= lim),
        "B 왕복 비용 평균 bp": (mean(b_cost), j["b_round_trip_cost_bps_max"], lambda v, lim: v <= lim),
    }
    return {
        "nights": len(done), "stopped": sum(r["how"] == "손절" for r in done),
        "a_nights": sum(r["method"] == "A" for r in exits), "b_nights": sum(r["method"] == "B" for r in exits),
        "b_maker_share": mean(b_maker), "tracking_mean": mean(te),
        "pnl_sum": sum(r["pnl"] for r in done),
        "checks": {k: {"value": v, "limit": lim, "ok": (ok(v, lim) if v is not None else None)}
                   for k, (v, lim, ok) in checks.items()},
        "risk": risk,
    }


# ---------------------------------------------------------------- DB·기준가
def kline_ref(symbol: str) -> Callable[[datetime], float | None]:
    """1분봉 시가 조회 (분 단위 캐시)."""
    from ..binance_data import fetch_klines
    cache: dict[datetime, float | None] = {}

    def ref(t: datetime) -> float | None:
        m = t.replace(second=0, microsecond=0)
        if m not in cache:
            ms = int(m.timestamp() * 1000)
            df = fetch_klines(symbol, "1m", ms, ms)
            cache[m] = float(df["open"].iloc[0]) if len(df) else None
        return cache[m]
    return ref


def compute(mode: str, conn: psycopg.Connection, ref: Callable[[datetime], float | None] | None = None,
            only: str | None = None) -> list[dict]:
    """exec.nights(done) 마다 지표 계산 → exec.night_metrics 덮어쓰기."""
    q = "SELECT * FROM exec.nights WHERE mode = %s AND phase = 'done'" + (" AND entry_date = %s" if only else "")
    cur = conn.execute(q, (mode, only) if only else (mode,))
    cols = [c.name for c in cur.description]
    nights = [dict(zip(cols, r)) for r in cur.fetchall()]
    out = []
    for nt in nights:
        spec = spec_for(nt["spec_version"])
        ref = ref or kline_ref(spec.raw["symbol"])
        fc = conn.execute("SELECT client_id, ts, leg, side, qty, price, fee, liquidity FROM exec.fills "
                          "WHERE mode = %s AND entry_date = %s ORDER BY ts", (mode, nt["entry_date"]))
        fills = [dict(zip([c.name for c in fc.description], r)) for r in fc.fetchall()]
        dc = conn.execute("SELECT leg, bid, ask, scheduled_at FROM exec.decisions WHERE mode = %s AND entry_date = %s",
                          (mode, nt["entry_date"])).fetchall()
        decisions = {leg: {"bid": b, "ask": a, "scheduled_at": s} for leg, b, a, s in dc}
        m = night_metrics(nt, fills, decisions, spec, ref)
        keys = [k for k in m if k != "entry_date"]
        conn.execute(
            f"INSERT INTO exec.night_metrics (mode, entry_date, {', '.join(keys)}, computed_at) "
            f"VALUES (%s, %s, {', '.join(['%s'] * len(keys))}, now()) ON CONFLICT (mode, entry_date) DO UPDATE SET "
            + ", ".join(f"{k}=EXCLUDED.{k}" for k in keys) + ", computed_at=now()",
            (mode, m["entry_date"], *[m[k] for k in keys]))
        out.append(m)
    return out


def load_metrics(mode: str, conn: psycopg.Connection) -> list[dict]:
    cur = conn.execute("SELECT * FROM exec.night_metrics WHERE mode = %s ORDER BY entry_date", (mode,))
    cols = [c.name for c in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


# ---------------------------------------------------------------- 출력
def _f(v, fmt="+.1f"):
    return "-" if v is None else format(v, fmt)


def night_line(m: dict) -> str:
    return (f"{m['entry_date']} [{m['method']}] {m['how']} | 실제 {_f(m['gross_bp'])}bp vs 백테스트 {_f(m['bt_gross_bp'])}bp "
            f"(차 {_f(m['tracking_bp'])}) | 슬리피지 진입 {_f(m['slip_entry_bp'])} 청산 {_f(m['slip_exit_bp'])} "
            f"수수료 {_f(m['fee_bp'])} → 왕복 {_f(m['cost_rt_bp'])}bp | maker {_f(m['maker_entry'], '.0%')}/{_f(m['maker_exit'], '.0%')} "
            f"| 손익 {_f(m['pnl'], '+.4f')}{'' if m['on_time'] else ' ⚠️ 정시 아님'}")


def summary_text(mode: str, rows: list[dict], s: dict) -> str:
    lines = [f"■ {mode}: {s['nights']}밤 (A {s['a_nights']} / B {s['b_nights']}, 손절 {s['stopped']}), "
             f"손익 합 {s['pnl_sum']:+.4f} USDT, B maker 비율 {_f(s['b_maker_share'], '.0%')}"]
    for k, c in s["checks"].items():
        mark = "⏳" if c["ok"] is None else ("✅" if c["ok"] else "❌")
        lines.append(f"  {mark} {k}: {_f(c['value'], '.2f')} (기준 {c['limit']})")
    if s.get("risk"):
        r = s["risk"]
        lines.append(f"  리스크 {r.get('status')} · 누적 {r.get('cumulative_pnl', 0):+.4f} · 연속 손실 {r.get('consecutive_losses', 0)}")
    lines += ["  " + night_line(m) for m in rows[-10:]]
    return "\n".join(lines)


def compare_text(conn: psycopg.Connection) -> str:
    rows = conn.execute(
        "SELECT l.entry_date, l.method, l.gross_bp, p.gross_bp, l.bt_gross_bp, l.cost_rt_bp, p.cost_rt_bp, "
        "l.maker_entry, p.maker_entry FROM exec.night_metrics l JOIN exec.night_metrics p "
        "ON p.entry_date = l.entry_date AND p.mode = 'paper' WHERE l.mode = 'live' ORDER BY l.entry_date").fetchall()
    out = ["밤 | 방식 | 라이브 gross | 페이퍼 gross | 백테스트 | 라이브 비용 | 페이퍼 비용 | maker 진입 L/P"]
    for d, m, lg, pg, bt, lc, pc, lm, pm in rows:
        out.append(f"{d} | {m} | {_f(lg)} | {_f(pg)} | {_f(bt)} | {_f(lc)} | {_f(pc)} | {_f(lm, '.0%')}/{_f(pm, '.0%')}")
    return "\n".join(out) if rows else "같은 밤 페이퍼·라이브 결과가 아직 없다"


def night_hook(mode: str, notify: Callable[[str], None], state_root: Path = STATE_ROOT,
               dsn: str | None = None) -> Callable[[str], None]:
    """스케줄러가 밤 확정 후 호출: 적재 → 그 밤 지표 → 텔레그램 한 줄 + Obsidian 일일 로그."""
    def hook(entry_date: str) -> None:
        with psycopg.connect(get_dsn(dsn), autocommit=True) as conn:
            ingest(mode, conn, state_root)
            ms = compute(mode, conn, only=entry_date)
            rows = load_metrics(mode, conn)
        if not ms:
            return
        risk_f = state_root / mode / "risk.json"
        risk = json.loads(risk_f.read_text()) if risk_f.exists() else None
        s = summarize(rows, spec_for(None), risk)
        notify(f"[overnight {mode} 리포트] {night_line(ms[0])}")
        try:
            from .. import obsidian
            obsidian.append_daily_log(f"```\n{summary_text(mode, rows, s)}\n```", heading=f"[WOO-99] 오버나잇 {mode} 리포트")
        except Exception:
            pass                                             # 볼트가 없어도 리포트는 DB·텔레그램에 남는다
    return hook


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="python -m src.overnight.report")
    ap.add_argument("--mode", choices=["paper", "live"], default="live")
    ap.add_argument("--compare", action="store_true", help="같은 밤 페이퍼 vs 라이브")
    ap.add_argument("--telegram", action="store_true")
    args = ap.parse_args(argv)
    with psycopg.connect(get_dsn(), autocommit=True) as conn:
        if args.compare:
            for m in ("paper", "live"):
                ingest(m, conn)
                compute(m, conn)
            print(compare_text(conn))
            return
        n = ingest(args.mode, conn)
        compute(args.mode, conn)
        rows = load_metrics(args.mode, conn)
    risk_f = STATE_ROOT / args.mode / "risk.json"
    s = summarize(rows, spec_for(None), json.loads(risk_f.read_text()) if risk_f.exists() else None)
    text = summary_text(args.mode, rows, s)
    print(f"(새로 적재 {n}줄)\n{text}")
    if args.telegram:
        from ..notify import TelegramNotifier
        tg = TelegramNotifier.from_env()
        if tg:
            tg.send(text)


if __name__ == "__main__":
    main()
