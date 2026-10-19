"""pastofi-1.3 엔진을 Binance 무기한 L1·aggTrade 로 그대로 돌려 본다 (KRX 전용 규칙은 무력화)."""
import sys, dataclasses, json
sys.path.insert(0, "/Users/woojin/Desktop/krx-hft")
import numpy as np, pandas as pd, psycopg
from pastofi.engine import SymbolEngine
from pastofi.params import DEFAULT

SYM = sys.argv[1]
SCALE = 1000.0                  # 소수 수량 → 정수 (on_trade 가 int 로 자른다). 호가 잔량도 같은 배율
THS = ['A', 'B']   # A: 익절2%/손절1%, B: 익절60/손절30
P = {th: dataclasses.replace(DEFAULT, move_bp=70.0, **({'tp_bp': 200.0, 'sl_bp': 100.0} if th == 'A' else {'tp_bp': 60.0, 'sl_bp': 30.0}), session_open=0, session_close=86400,
                             entry_start=0, entry_end=86400 - 300, fut_min_rem=0, fut_gap_ticks=10**9, fut_limit_bp=1e9, vi_pad=0) for th in THS}
conn = psycopg.connect("postgresql:///autocoin")
days = [r[0] for r in conn.execute(
    "select distinct (exchange_ts at time zone 'UTC')::date from market.trades where symbol=%s order by 1", (SYM,))]
out, sigsec = [], {th: 0 for th in THS}
for d in days[1:-1]:            # 첫날·마지막날(부분일) 제외
    lo, hi = f"{d} 00:00+00", f"{d + pd.Timedelta(days=1)} 00:00+00"
    q = pd.read_sql("select extract(epoch from exchange_ts) t, bid_px_1 b, bid_qty_1 bq, ask_px_1 a, ask_qty_1 aq "
                    "from market.book_top5 where symbol=%s and exchange_ts>=%s and exchange_ts<%s order by exchange_ts, update_id",
                    conn, params=(SYM, lo, hi))
    tr = pd.read_sql("select floor(extract(epoch from exchange_ts))::bigint s, qty, is_buyer_maker m "
                     "from market.trades where symbol=%s and exchange_ts>=%s and exchange_ts<%s", conn, params=(SYM, lo, hi))
    if len(q) < 1000:
        continue
    base = pd.Timestamp(lo).timestamp()
    q["t"] -= base; tr["s"] -= int(base)
    q = q[(q[["b", "bq", "a", "aq"]].diff().abs().sum(axis=1) > 0) | (q.index == q.index[0])]   # L1 변화만
    tr["side"] = np.where(tr.m, -1, 1)
    vs = tr.groupby(["s", "side"]).qty.sum().reset_index()
    engs = {th: SymbolEngine(P[th], fut_base=float((q.b.iloc[0] + q.a.iloc[0]) / 2), market="KOSPI") for th in THS}
    for th, e in engs.items():  # |past| 문턱을 넘은 초 수 (OFI 조건 전)
        f0 = e._features
        def f(T, f0=f0, th=th):
            r = f0(T)
            if r is not None: sigsec[th] += 1
            return r
        e._features = f
    qa = q.to_numpy(); va = vs.to_numpy()
    qi = vi = 0
    for T in range(0, 86400):
        while qi < len(qa) and qa[qi, 0] <= T:
            t, b, bq, a, aq = qa[qi]
            for e in engs.values():
                e.on_quote(t, b, bq * SCALE, a, aq * SCALE, True, True)
                e.on_fut_quote(t, b, bq * SCALE, a, aq * SCALE)
            qi += 1
        while vi < len(va) and va[vi, 0] <= T - 1:
            for e in engs.values():
                e.on_trade(int(va[vi, 0]), int(round(va[vi, 2] * SCALE)), int(va[vi, 1]))
            vi += 1
        for e in engs.values():
            e.advance(T)
    for th, e in engs.items():
        e.finish()
        for x in e.trades:
            out.append(dict(sym=SYM, day=str(d), th=th, **x.to_dict()))
    print(SYM, d, {th: len(e.trades) for th, e in engs.items()}, flush=True)
pd.DataFrame(out).to_csv(f"/tmp/pastofi_crypto/all_trades_{SYM}.csv", index=False)
json.dump({"days": len(days) - 2, "sigsec": sigsec}, open(f"/tmp/pastofi_crypto/all_meta_{SYM}.json", "w"))
