"""DB 없이 PostgresSink 변환·장애 처리 검증."""
import json

import psycopg

from src.storage.pg_sink import TOP_COLS, PostgresSink, event_row, top_row, trade_row

T = 1789478631342


def _trade(a):
    return {"kind": "trade", "symbol": "EWYUSDT", "recv_ts": T + 50, "e": "aggTrade", "E": T + 1,
            "s": "EWYUSDT", "a": a, "p": "178.5", "q": "2", "f": a, "l": a, "T": T, "m": True}


def test_row_conversions():
    tr = trade_row(_trade(7))
    assert tr[1:8] == ("EWYUSDT", 7, 178.5, 2.0, 7, 7, True) and tr[-1] == "ws"
    top = top_row({"kind": "book_top", "symbol": "EWYUSDT", "recv_ts": T, "E": T, "T": T - 2, "u": 9,
                   "bids": [[178.5, 1.0], [178.4, 2.0]], "asks": [[178.6, 3.0]]})
    assert len(top) == len(TOP_COLS)
    row = dict(zip(TOP_COLS, top))
    assert row["bid_px_2"] == 178.4 and row["bid_px_3"] is None and row["ask_qty_1"] == 3.0
    assert row["exchange_ts"].timestamp() * 1000 == T - 2
    ev = event_row({"kind": "snapshot", "symbol": "EWYUSDT", "recv_ts": T, "lastUpdateId": 5,
                    "bids": [[1, 1]] * 3, "asks": [[2, 1]]})
    assert json.loads(ev[4]) == {"lastUpdateId": 5, "bid_levels": 3, "ask_levels": 1}


def test_db_failure_never_raises_and_spills(tmp_path):
    attempts, spilled = [], []

    def down(*a, **k):
        attempts.append(1)
        raise psycopg.OperationalError("connection refused")

    sink = PostgresSink("postgresql:///nope", flush_rows=1, max_buffer_rows=3, spill_dir=tmp_path,
                        connect=down, clock=lambda: 0.0,
                        on_spill=lambda path, n, why: spilled.append((path, n)))
    for a in range(1, 5):
        sink.write(_trade(a))
    sink.write({"kind": "depth", "symbol": "EWYUSDT"})     # 저장 대상 아님 → 무시
    assert len(attempts) == 1          # 첫 실패 후 백오프 동안 재시도 안 함
    assert sink.failures == 1 and sink.pending == 0
    path, n = spilled[0]
    assert n == 4 and len(path.read_text().splitlines()) == 4


def test_close_spills_when_db_still_down(tmp_path):
    def down(*a, **k):
        raise psycopg.OperationalError("down")

    spilled = []
    sink = PostgresSink("postgresql:///nope", flush_rows=100, flush_seconds=999, spill_dir=tmp_path,
                        connect=down, on_spill=lambda p, n, why: spilled.append(n))
    sink.write(_trade(1))
    sink.close()
    assert spilled == [1]


def test_malformed_record_dropped_not_fatal(tmp_path):
    class FakeConn:
        closed = False

        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *e):
            return False

        def executemany(self, sql, rows):
            self.rows = rows

        def commit(self):
            pass

        def close(self):
            pass

    conn = FakeConn()
    sink = PostgresSink("x", flush_rows=100, flush_seconds=999, connect=lambda *a, **k: conn)
    bad = _trade(1)
    del bad["p"]
    sink.write(bad)
    sink.write(_trade(2))
    assert sink.flush() and sink.dropped == 1 and len(conn.rows) == 1
