"""페이퍼 장부 v1 → v2 변환 (한 번 쓰고 마는 스크립트, 사용자 요청 2026-10-29).

v1(종목마다 최소 명목)으로 체결된 페이퍼 거래를 v2 수량 규칙(4종목 동일 명목)으로 다시 잡는다.
    수량   = v2 sized() — 진입 종목은 신호가, 나머지 3종목은 신호 초 직후 첫 aggTrade 가격 (Binance 공개 REST)
    체결가 = v1 체결가 그대로 (5호가 VWAP 를 다시 만들 호가가 없다 — 명목 $81 은 거의 늘 1호가 안이다)
    수수료 = 테이커 4bp × 양쪽 명목, 손익 = (진입가 − 청산가) × 수량
원본은 data/pastofi/paper-v1/ 에 그대로 복사해 두고, data/pastofi/paper/ 를 v2 장부로 바꾼다.
v1 값은 거래마다 v1 키로 남긴다 (DB 는 detail jsonb).

    python -m src.strategies.pastofi_crypto.migrate_v2              # 미리 보기
    python -m src.strategies.pastofi_crypto.migrate_v2 --apply      # 페이퍼 에이전트를 멈춘 뒤에
"""
from __future__ import annotations

import argparse
import json
import shutil
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .runner import STATE_ROOT
from .spec import SPEC_V1, Spec, SymbolFilters, sized
from .store import Store, read_jsonl

V1_KEYS = ("qty", "gross_usd", "fees_usd", "net_usd", "spec_version")


def price_at(rest, symbol: str, t_sig: float) -> float:
    rows = rest.get("/fapi/v1/aggTrades", {"symbol": symbol, "startTime": int(t_sig * 1000), "limit": 1})
    if not rows:
        raise RuntimeError(f"{symbol} {t_sig} 이후 체결이 없다")
    return float(rows[0]["p"])


def convert(trades: list[dict], spec: Spec, filters: dict, rest, taker_bps: float) -> list[dict]:
    out = []
    for t in sorted(trades, key=lambda r: r["t_sig"]):
        prices = {s: t["assumed_in"] if s == t["symbol"] else price_at(rest, s, t["t_sig"]) for s in spec.symbols}
        q = sized(spec, t["symbol"], prices, filters)
        gross = (t["entry_px"] - t["exit_px"]) * q
        fees = q * (t["entry_px"] + t["exit_px"]) * taker_bps / 1e4
        out.append({**t, "qty": q, "gross_usd": gross, "fees_usd": fees, "net_usd": gross - fees,
                    "spec_version": spec.version, "v1": {k: t.get(k) for k in V1_KEYS},
                    "v2_prices": prices})
    return out


def rebuild_state(state: dict, trades: list[dict], spec: Spec) -> dict:
    realized = defaultdict(lambda: {"net": 0.0, "n": 0, "win": 0, "loss": 0})
    for t in trades:
        r = realized[t["day"]]
        r["net"] += t["net_usd"]
        r["n"] += 1
        r["win" if t["net_usd"] > 0 else "loss"] += 1
    return {**state, "spec_version": spec.version, "cum_net": sum(t["net_usd"] for t in trades),
            "cum_gross": sum(t["gross_usd"] for t in trades), "cum_fees": sum(t["fees_usd"] for t in trades),
            "n_trades": len(trades), "realized": dict(realized)}


def rescale_paper_exchange(path: Path, ratio: dict[str, float], taker_bps: float) -> None:
    """페이퍼 거래소 시뮬 상태의 주문·체결 수량을 거래별 배율로 바꾼다 (포지션은 0 이어야 한다)."""
    d = json.loads(path.read_text())
    if abs(d["position"]["qty"]) > 1e-12:
        raise SystemExit(f"{path.name}: 포지션이 열려 있다 — 청산 뒤에 변환하라")
    for o in d["orders"]:
        k = ratio.get(o["client_id"].rsplit("-", 1)[0])
        if k:
            o["qty"] = round(o["qty"] * k, 8)
            o["filled_qty"] = round(o["filled_qty"] * k, 8)
    pnl = fees = 0.0
    for f in d["fills"]:
        k = ratio.get(f["client_id"].rsplit("-", 1)[0])
        if k:
            f["qty"] = round(f["qty"] * k, 8)
            f["fee"] = f["qty"] * f["price"] * taker_bps / 1e4
        pnl += (1 if f["side"] == "SELL" else -1) * f["qty"] * f["price"]
        fees += f["fee"]
    d["position"]["realized_pnl"], d["position"]["fees"] = pnl, fees
    path.write_text(json.dumps(d, ensure_ascii=False, indent=1))


def main() -> None:
    from ...exchange.rest import FuturesRestClient
    ap = argparse.ArgumentParser(prog="python -m src.strategies.pastofi_crypto.migrate_v2")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--state-root", type=Path, default=STATE_ROOT)
    args = ap.parse_args()

    spec, v1 = Spec.load(), Spec.load(SPEC_V1)
    sdir, archive = args.state_root / "paper", args.state_root / "paper-v1"
    state = json.loads((sdir / "state.json").read_text())
    if state["spec_version"] != v1.version:
        raise SystemExit(f"장부 스펙이 {state['spec_version']} — v1 장부만 변환한다")
    if any(state["positions"].values()):
        raise SystemExit("열린 포지션이 있다 — 청산 뒤에 변환하라")
    rest = FuturesRestClient()
    info = rest.get("/fapi/v1/exchangeInfo")
    filters = {s: SymbolFilters.from_exchange_info(info, s) for s in spec.symbols}
    taker = float(spec.raw["fees_bps"]["taker"])
    old = read_jsonl(sdir / "trades.jsonl", ("trade_id",))
    new = convert(old, spec, filters, rest, taker)

    print(f"{'거래':<22} {'수량 v1→v2':>18} {'명목 v1→v2':>18} {'순손익 v1→v2':>22}")
    for t in new:
        a = t["v1"]
        print(f"{t['trade_id']:<22} {a['qty']:>8g} → {t['qty']:<7g} ${a['qty'] * t['entry_px']:>6.2f} → ${t['qty'] * t['entry_px']:<7.2f}"
              f" {a['net_usd']:>+9.4f} → {t['net_usd']:+.4f}")
    st2 = rebuild_state(state, new, spec)
    print(f"누적 순손익 {state['cum_net']:+.4f} → {st2['cum_net']:+.4f}  (수수료 {st2['cum_fees']:.4f}, {st2['n_trades']}건)")
    if not args.apply:
        print("미리 보기 — 반영하려면 --apply")
        return

    if archive.exists():
        raise SystemExit(f"{archive} 가 이미 있다")
    shutil.copytree(sdir, archive)
    with (sdir / "trades.jsonl").open("w", encoding="utf-8") as fh:
        for t in new:
            fh.write(json.dumps(t, ensure_ascii=False, default=str) + "\n")
    (sdir / "state.json").write_text(json.dumps(st2, ensure_ascii=False, indent=1))
    ratio = {t["entry_cid"].rsplit("-", 1)[0]: t["qty"] / t["v1"]["qty"] for t in new}
    for s in spec.symbols:
        p = sdir / f"paper_{s}.json"
        if p.exists():
            rescale_paper_exchange(p, ratio, taker)
    for p in sorted((sdir / "reconcile").glob("*.json")):
        r = json.loads(p.read_text())
        r["total"]["net_usd"] = sum(t["net_usd"] for t in new if t["day"] == r["day"])
        p.write_text(json.dumps(r, ensure_ascii=False, indent=1))
    store = Store("paper", sdir, use_db=False)
    store.event({"event": "migrate", "from": v1.version, "to": spec.version, "spec_sha256": spec.sha256,
                 "archive": str(archive), "n_trades": len(new), "cum_net_v1": state["cum_net"], "cum_net_v2": st2["cum_net"],
                 "at": datetime.now(timezone.utc).isoformat()})
    n = store.sync_all()
    print(f"반영 완료 — 원본 {archive}, DB {n}행 upsert")


if __name__ == "__main__":
    main()
