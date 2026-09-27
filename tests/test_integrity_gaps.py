"""긴 공백(하루 오프라인 등) 감지·백필 경로. DB 통합 테스트 — 서버가 없으면 skip."""
from datetime import datetime, timedelta, timezone

from src import integrity
from tests.test_storage_db import BASE, _load, _trade, conn, dsn  # noqa: F401 (fixtures)

BASE_DT = datetime.fromtimestamp(BASE / 1000, tz=timezone.utc)


def test_gap_longer_than_window_is_detected(conn, dsn):
    # 30시간 전 마지막 체결(a=1) → 공백 → 현재 구간 안 체결(a=5): 24시간 구간으로 검사해도 잡혀야 함
    _load(dsn, [_trade(1, BASE - 30 * 3_600_000), _trade(5, BASE), _trade(6, BASE + 1_000)])
    r = integrity.run_checks(conn, "EWYUSDT", BASE_DT - timedelta(hours=24), BASE_DT + timedelta(hours=1))
    assert [(g[0], g[1]) for g in r["agg_gaps"]] == [(2, 4)]


def test_oversized_gap_goes_to_archive(conn, monkeypatch):
    calls = []

    def fake_archive(conn_, symbol, from_id, to_id, start_ts, end_ts, **kw):
        calls.append((from_id, to_id))
        return to_id - from_id + 1

    class NoRest:
        def get(self, *a, **k):
            raise AssertionError("한도 초과 구간은 REST 로 가면 안 됨")

    monkeypatch.setattr(integrity.archive, "backfill_gap", fake_archive)
    gaps = [(3, 250_002, BASE_DT, BASE_DT + timedelta(hours=24))]
    assert integrity.backfill_agg_gaps(conn, NoRest(), "EWYUSDT", gaps, max_ids_per_gap=100_000) == 250_000
    assert calls == [(3, 250_002)]
