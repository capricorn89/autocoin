"""로컬 PostgreSQL+TimescaleDB 통합 테스트. 서버가 없으면 skip.

DSN: AUTOCOIN_TEST_PG_DSN (기본 postgresql:///autocoin_test — 운영 DB 와 분리)
"""
import os
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from src.integrity import backfill_agg_gaps, render_markdown, run_checks
from src.storage import db
from src.storage.pg_sink import PostgresSink

TEST_DSN = os.getenv("AUTOCOIN_TEST_PG_DSN", "postgresql:///autocoin_test")
BASE = 1789478631342
START = datetime.fromtimestamp(BASE / 1000, tz=timezone.utc) - timedelta(hours=1)
END = START + timedelta(hours=3)


@pytest.fixture(scope="module")
def dsn():
    try:
        db.ensure_database(TEST_DSN)
        info = db.apply_schema(TEST_DSN)
    except psycopg.OperationalError as e:
        pytest.skip(f"PostgreSQL 사용 불가: {e}")
    assert info["timescaledb"]
    return TEST_DSN


@pytest.fixture
def conn(dsn):
    with psycopg.connect(dsn, autocommit=True) as c:
        c.execute("TRUNCATE market.trades, market.book_top5, market.collector_events")
        yield c


def _trade(a, t, p=178.5):
    return {"kind": "trade", "symbol": "EWYUSDT", "recv_ts": t + 50, "e": "aggTrade", "E": t + 1,
            "s": "EWYUSDT", "a": a, "p": str(p), "q": "1", "f": a * 10, "l": a * 10, "T": t, "m": False}


def _top(u, t, bid=178.50, ask=178.51):
    return {"kind": "book_top", "symbol": "EWYUSDT", "recv_ts": t + 30, "E": t + 1, "T": t, "u": u,
            "bids": [[round(bid - 0.01 * i, 2), 1.0] for i in range(5)],
            "asks": [[round(ask + 0.01 * i, 2), 2.0] for i in range(5)]}


def _load(dsn, records):
    sink = PostgresSink(dsn, flush_rows=10 ** 6, flush_seconds=10 ** 6)
    for r in records:
        sink.write(r)
    sink.close()
    assert sink.failures == 0
    return sink


def test_schema_is_idempotent(dsn):
    db.apply_schema(dsn)
    db.apply_schema(dsn)


def test_hypertables_partitioned_by_exchange_ts(conn):
    rows = dict(conn.execute("""
        SELECT hypertable_name, column_name FROM timescaledb_information.dimensions
        WHERE hypertable_schema = 'market'""").fetchall())
    assert rows["trades"] == "exchange_ts" and rows["book_top5"] == "exchange_ts"


def test_sink_inserts_and_dedupes(conn, dsn):
    _load(dsn, [_trade(1, BASE), _trade(2, BASE + 1000), _trade(2, BASE + 1000), _top(10, BASE),
                {"kind": "gap", "stream": "aggTrade", "symbol": "EWYUSDT", "recv_ts": BASE,
                 "detail": "누락", "from_id": 3, "to_id": 4},
                {"kind": "depth", "symbol": "EWYUSDT", "recv_ts": BASE}])
    assert conn.execute("SELECT count(*) FROM market.trades").fetchone()[0] == 2
    row = conn.execute("SELECT bid_px_1, bid_px_5, ask_px_1, ask_qty_5 FROM market.book_top5").fetchone()
    assert row == (178.5, pytest.approx(178.46), 178.51, 2.0)
    assert conn.execute("SELECT kind, detail->>'from_id' FROM market.collector_events").fetchone() == ("gap", "3")


def test_integrity_detects_problems_and_backfills(conn, dsn):
    _load(dsn, [
        _trade(1, BASE), _trade(2, BASE + 1_000),
        _trade(5, BASE + 121_000),              # id 3~4 누락, 약 2분 무체결
        _trade(6, BASE + 120_500),              # id 는 증가하는데 시각은 역전
        _top(10, BASE), _top(11, BASE + 500, bid=178.52, ask=178.51),   # 호가 교차
        _top(12, BASE + 30_500),                # 30초 5호가 결측
        {"kind": "clock", "recv_ts": BASE, "offset_ms": -60.0, "rtt_ms": 40.0, "samples": 5},
    ])
    r = run_checks(conn, "EWYUSDT", START, END, trade_gap_s=60, book_gap_s=10)
    assert [(g[0], g[1]) for g in r["agg_gaps"]] == [(3, 4)] and r["agg_missing_ids"] == 2
    assert r["trade_ts_inversions"] == 1
    assert r["trade_silence_count"] == 1
    assert r["book_crossed"] == 1 and r["book_silence_count"] == 1 and r["book_ts_inversions"] == 0
    assert r["clock_offset_ms"] == -60.0
    body, summary = render_markdown(r)
    assert "❌ 이상" in body and summary["결과_체결id누락"] == 2

    class FakeRest:
        def get(self, path, params):
            assert path == "/fapi/v1/aggTrades" and params["fromId"] == 3
            return [{"a": 3, "p": "178.5", "q": "1", "f": 30, "l": 30, "T": BASE + 2_000, "m": True},
                    {"a": 4, "p": "178.5", "q": "1", "f": 40, "l": 40, "T": BASE + 3_000, "m": True},
                    {"a": 5, "p": "178.5", "q": "1", "f": 50, "l": 50, "T": BASE + 121_000, "m": True}]

    assert backfill_agg_gaps(conn, FakeRest(), "EWYUSDT", r["agg_gaps"]) == 2
    r2 = run_checks(conn, "EWYUSDT", START, END)
    assert r2["agg_gaps"] == []
    assert conn.execute("SELECT count(*) FROM market.trades WHERE source = 'rest_backfill'").fetchone()[0] == 2
