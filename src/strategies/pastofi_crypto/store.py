"""거래·신호 기록: jsonl(원본) + exec.pastofi_* (적재본).

jsonl 은 같은 키가 여러 줄 나올 수 있다(신호 open → 청산). 읽을 때 마지막 줄이 이긴다.
DB 적재는 별도 스레드에서 upsert 한다. DB 가 죽어 있어도 매매는 계속하고, 다음 시작 때 sync_all() 로 다시 넣는다.
"""
from __future__ import annotations

import json
import logging
import queue
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)
SCHEMA = Path(__file__).with_name("schema.sql")

TRADE_COLS = ("mode", "trade_id", "symbol", "day", "t_sig", "entry_ts", "exit_ts", "side", "qty", "entry_px", "exit_px",
              "assumed_in", "assumed_out", "reason", "gross_usd", "fees_usd", "net_usd", "pnl_bp", "assumed_bp",
              "slip_in_bp", "slip_out_bp", "past_bp", "ofi_ratio", "entry_cid", "exit_cid", "spec_version", "detail")
SIGNAL_COLS = ("mode", "symbol", "t_sig", "day", "status", "skip_reason", "p_in", "t_out", "p_out", "reason",
               "engine_bp", "past_bp", "ofi_ratio", "detail")


def _ts(x):
    if x is None or isinstance(x, datetime):
        return x
    return datetime.fromtimestamp(float(x), tz=timezone.utc)


def _upsert_sql(table: str, cols: tuple, key: tuple) -> str:
    upd = ", ".join(f"{c} = EXCLUDED.{c}" for c in cols if c not in key)
    return (f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({', '.join(['%s'] * len(cols))}) "
            f"ON CONFLICT ({', '.join(key)}) DO UPDATE SET {upd}")


TRADE_SQL = _upsert_sql("exec.pastofi_trades", TRADE_COLS, ("mode", "trade_id"))
SIGNAL_SQL = _upsert_sql("exec.pastofi_signals", SIGNAL_COLS, ("mode", "symbol", "t_sig"))


def trade_row(r: dict) -> tuple:
    detail = {k: v for k, v in r.items() if k not in TRADE_COLS}
    vals = {**r, "t_sig": _ts(r["t_sig"]), "entry_ts": _ts(r.get("entry_ts")), "exit_ts": _ts(r.get("exit_ts")),
            "detail": json.dumps(detail, ensure_ascii=False, default=str)}
    return tuple(vals.get(c) for c in TRADE_COLS)


def signal_row(r: dict) -> tuple:
    detail = {k: v for k, v in r.items() if k not in SIGNAL_COLS}
    vals = {**r, "t_sig": _ts(r["t_sig"]), "t_out": _ts(r.get("t_out")),
            "detail": json.dumps(detail, ensure_ascii=False, default=str)}
    return tuple(vals.get(c) for c in SIGNAL_COLS)


def read_jsonl(path: Path, key: tuple) -> list[dict]:
    if not path.exists():
        return []
    out: dict = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            try:
                r = json.loads(line)
            except json.JSONDecodeError:
                continue                                 # 쓰다 죽은 마지막 줄
            out[tuple(r.get(k) for k in key)] = r
    return list(out.values())


def ensure_schema(dsn: str | None = None) -> None:
    import psycopg
    from ...storage.db import get_dsn
    with psycopg.connect(get_dsn(dsn)) as conn:
        conn.execute(SCHEMA.read_text(encoding="utf-8"))


class Store:
    def __init__(self, mode: str, state_dir: Path, dsn: str | None = None, use_db: bool = True):
        self.mode = mode
        self.dir = Path(state_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.trades_file = self.dir / "trades.jsonl"
        self.signals_file = self.dir / "signals.jsonl"
        self.events_file = self.dir / "events.jsonl"
        self.dsn = dsn
        self._lock = threading.Lock()
        self._q: queue.Queue | None = None
        self.db_errors = 0
        if use_db:
            self._q = queue.Queue()
            threading.Thread(target=self._db_loop, name="pastofi-db", daemon=True).start()

    # ------------------------------------------------------------ 쓰기
    def _append(self, path: Path, rec: dict) -> None:
        with self._lock, path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def trade(self, rec: dict) -> None:
        rec = {"mode": self.mode, **rec}
        self._append(self.trades_file, rec)
        if self._q is not None:
            self._q.put(("trade", rec))

    def signal(self, rec: dict) -> None:
        rec = {"mode": self.mode, **rec}
        self._append(self.signals_file, rec)
        if self._q is not None:
            self._q.put(("signal", rec))

    def event(self, rec: dict) -> None:
        self._append(self.events_file, {"logged_at": datetime.now(timezone.utc).isoformat(), "mode": self.mode, **rec})

    # ------------------------------------------------------------ 읽기
    def trades(self) -> list[dict]:
        return read_jsonl(self.trades_file, ("trade_id",))

    def signals(self) -> list[dict]:
        return read_jsonl(self.signals_file, ("symbol", "t_sig"))

    # ------------------------------------------------------------ DB
    def sync_all(self) -> int:
        """jsonl 전부를 DB 에 다시 넣는다 (시작 시, 멱등)."""
        import psycopg
        from ...storage.db import get_dsn
        ensure_schema(self.dsn)
        tr, sg = self.trades(), self.signals()
        with psycopg.connect(get_dsn(self.dsn)) as conn, conn.cursor() as cur:
            if tr:
                cur.executemany(TRADE_SQL, [trade_row(r) for r in tr])
            if sg:
                cur.executemany(SIGNAL_SQL, [signal_row(r) for r in sg])
        return len(tr) + len(sg)

    def _db_loop(self) -> None:
        import psycopg
        from ...storage.db import get_dsn
        conn = None
        while True:
            kind, rec = self._q.get()
            try:
                if conn is None or conn.closed:
                    conn = psycopg.connect(get_dsn(self.dsn), autocommit=True)
                conn.execute(TRADE_SQL if kind == "trade" else SIGNAL_SQL,
                             trade_row(rec) if kind == "trade" else signal_row(rec))
            except Exception as e:                       # DB 장애는 매매를 막지 않는다 (jsonl 이 원본)
                self.db_errors += 1
                log.error("pastofi DB 적재 실패 (jsonl 에는 남음, 다음 시작 때 다시 넣는다): %s", str(e).splitlines()[0][:200])
                try:
                    conn and conn.close()
                except Exception:
                    pass
                conn = None
