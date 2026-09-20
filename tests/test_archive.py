"""아카이브 백필: ZIP 파싱, id 필터, UTC 날짜 범위 (네트워크 없이)."""
from __future__ import annotations

import io
import zipfile
from datetime import date, datetime, timedelta, timezone

from src.archive import _days, backfill_gap, daily_agg_trades, parse_zip

HEADER = "agg_trade_id,price,quantity,first_trade_id,last_trade_id,transact_time,is_buyer_maker"
ROWS = ["100,182.57,30.56,47217222,47217241,1789689600017,true",
        "101,182.56,40.71,47217242,47217260,1789689600039,false"]


def make_zip(lines: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("EWYUSDT-aggTrades-2026-09-18.csv", "\n".join(lines) + "\n")
    return buf.getvalue()


class FakeCursor:
    def __init__(self, sink: list) -> None:
        self.sink = sink

    def __enter__(self) -> "FakeCursor":
        return self

    def __exit__(self, *a) -> bool:
        return False

    def executemany(self, sql: str, rows) -> None:
        self.sink.extend(rows)


class FakeConn:
    def __init__(self) -> None:
        self.rows: list = []

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.rows)


def test_parse_zip_maps_rest_keys() -> None:
    out = parse_zip(make_zip([HEADER] + ROWS))
    assert [t["a"] for t in out] == [100, 101]
    assert out[0]["m"] is True and out[1]["m"] is False
    assert out[0]["T"] == 1789689600017


def test_parse_zip_without_header() -> None:
    assert len(parse_zip(make_zip(ROWS))) == 2


def test_backfill_gap_filters_ids_and_tags_source() -> None:
    conn = FakeConn()
    blob = make_zip([HEADER] + ROWS)
    ts = datetime(2026, 9, 18, 12, tzinfo=timezone.utc)
    n = backfill_gap(conn, "EWYUSDT", 101, 101, ts, ts, cache_dir=None, download=lambda url: blob)
    assert n == 1 and len(conn.rows) == 1
    assert conn.rows[0][2] == 101            # agg_id
    assert conn.rows[0][-1] == "archive"     # source


def test_missing_archive_file_is_not_fatal() -> None:
    assert daily_agg_trades("EWYUSDT", date(2026, 9, 18), cache_dir=None,
                            download=lambda url: None) == []


def test_days_spans_utc_dates() -> None:
    a = datetime(2026, 9, 18, 23, tzinfo=timezone.utc)
    assert [d.isoformat() for d in _days(a, a + timedelta(hours=2))] == ["2026-09-18", "2026-09-19"]
