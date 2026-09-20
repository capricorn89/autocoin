"""바이낸스 공개 아카이브(data.binance.vision)에서 과거 체결을 받아 갭을 메운다.

REST /fapi/v1/aggTrades 는 최근 2일만 조회할 수 있어(-4166) 그보다 오래된 누락 구간은
복구할 수 없다. 아카이브는 일 단위 aggTrades ZIP 을 제공하므로 이를 대신 사용한다.
CSV 컬럼은 REST 응답과 1:1 대응되며(agg_trade_id=a, transact_time=T ...) 같은 dict 로 바꿔
pg_sink.trade_row 에 그대로 넘긴다. 적재는 ON CONFLICT DO NOTHING 이라 재실행해도 안전하다.

당일 파일은 다음 날 올라오므로, 아직 없는 날짜는 빈 목록을 돌려준다.
"""
from __future__ import annotations

import csv
import io
import logging
import zipfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import requests

from .storage.pg_sink import TRADE_SQL, trade_row

log = logging.getLogger(__name__)

ARCHIVE_URL = ("https://data.binance.vision/data/futures/um/daily/aggTrades/"
               "{symbol}/{symbol}-aggTrades-{day}.zip")
CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "archive"
SOURCE = "archive"
_HEADER = "agg_trade_id"


def _download(url: str, timeout: float = 120.0) -> bytes | None:
    r = requests.get(url, timeout=timeout)
    if r.status_code == 404:
        return None
    r.raise_for_status()
    return r.content


def parse_zip(blob: bytes) -> list[dict]:
    """aggTrades 일간 ZIP → REST 와 같은 키({a,p,q,f,l,T,m})의 dict 목록."""
    out: list[dict] = []
    with zipfile.ZipFile(io.BytesIO(blob)) as z:
        with z.open(z.namelist()[0]) as fh:
            for row in csv.reader(io.TextIOWrapper(fh, encoding="utf-8")):
                if not row or row[0].startswith(_HEADER):
                    continue
                out.append({"a": int(row[0]), "p": row[1], "q": row[2], "f": int(row[3]),
                            "l": int(row[4]), "T": int(row[5]),
                            "m": row[6].strip().lower() == "true"})
    return out


def daily_agg_trades(symbol: str, day: date, cache_dir: Path | None = CACHE_DIR,
                     download: Callable[[str], bytes | None] = _download) -> list[dict]:
    """하루치 체결. 캐시가 있으면 재사용하고, 아카이브에 파일이 없으면 빈 목록."""
    path = cache_dir / f"{symbol}-aggTrades-{day.isoformat()}.zip" if cache_dir else None
    if path is not None and path.exists():
        return parse_zip(path.read_bytes())
    blob = download(ARCHIVE_URL.format(symbol=symbol, day=day.isoformat()))
    if blob is None:
        log.warning("%s %s 아카이브 파일 없음 (아직 미게시일 수 있음)", symbol, day)
        return []
    if path is not None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(blob)
    return parse_zip(blob)


def _days(start_ts: datetime, end_ts: datetime) -> list[date]:
    """구간이 걸친 UTC 날짜들 (아카이브가 UTC 일 단위로 나뉘므로)."""
    a = start_ts.astimezone(timezone.utc).date()
    b = end_ts.astimezone(timezone.utc).date()
    return [a + timedelta(days=i) for i in range((b - a).days + 1)]


def backfill_gap(conn, symbol: str, from_id: int, to_id: int,
                 start_ts: datetime, end_ts: datetime, **kw) -> int:
    """누락 id 구간 [from_id, to_id] 을 아카이브에서 찾아 적재한 행 수."""
    rows = [trade_row(t, symbol=symbol, source=SOURCE)
            for day in _days(start_ts, end_ts)
            for t in daily_agg_trades(symbol, day, **kw)
            if from_id <= t["a"] <= to_id]
    if rows:
        with conn.cursor() as cur:
            cur.executemany(TRADE_SQL, rows)
    return len(rows)
