"""체결 로그·판정 지표 (WOO-99).

순수 계산은 DB 없이, 적재는 운영과 분리된 autocoin_test DB 로 (없으면 skip).
"""
import os
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest

from src.overnight.report import (compute, ingest, leg_of, load_metrics, night_line, night_metrics,
                                  summarize, summary_text)
from src.overnight.spec import SPEC_PATH, Spec
from src.storage import db

from test_overnight_runner import history, kst, make, night   # 밤 시뮬레이션 재사용

SPEC = Spec.load()
UTC = timezone.utc
TEST_DSN = os.getenv("AUTOCOIN_TEST_PG_DSN", "postgresql:///autocoin_test")


def fill(leg, side, qty, price, fee, liq, ts):
    return {"client_id": f"on-20261012-{leg}0", "leg": leg, "side": side, "qty": qty, "price": price,
            "fee": fee, "liquidity": liq, "ts": ts}


def refs(mapping):
    return lambda t: mapping.get(t.replace(second=0, microsecond=0).astimezone(UTC))


def test_leg_of():
    assert leg_of("on-20261013-e0") == ("2026-10-13", "e")
    assert leg_of("on-20261013-x99") == ("2026-10-13", "x")
    assert leg_of("order:123") == (None, None)


def test_metrics_market_night():
    night_ = {"entry_date": "2026-10-12", "method": "A", "pnl": 0.9}
    fills = [fill("e", "BUY", 1.0, 100.02, 0.040, "TAKER", kst("2026-10-12", "15:30:01")),
             fill("x", "SELL", 1.0, 100.95, 0.040, "TAKER", kst("2026-10-13", "08:59:01"))]
    ref = refs({kst("2026-10-12", "15:30"): 100.0, kst("2026-10-13", "08:59"): 101.0,
                kst("2026-10-13", "09:00"): 101.2})
    dec = {"e": {"bid": 100.0, "ask": 100.01}, "x": {"bid": 100.99, "ask": 101.0}}
    m = night_metrics(night_, fills, dec, SPEC, ref)
    assert m["slip_entry_bp"] == pytest.approx(2.0)                    # 100.02 vs 100.00
    assert m["slip_exit_bp"] == pytest.approx((1 - 100.95 / 101.0) * 1e4)
    assert m["slip_entry_mid_bp"] == pytest.approx((100.02 / 100.005 - 1) * 1e4)
    assert m["fee_bp"] == pytest.approx(0.08 / 100.02 * 1e4)
    assert m["cost_rt_bp"] == pytest.approx(m["slip_entry_bp"] + m["slip_exit_bp"] + m["fee_bp"])
    assert m["bt_gross_bp"] == pytest.approx((101.2 / 100.0 - 1) * 1e4)   # 백테스트는 09:00 시가
    assert m["tracking_bp"] == pytest.approx(m["gross_bp"] - m["bt_gross_bp"])
    assert m["on_time"] and m["maker_entry"] == 0 and m["how"] == "청산"


def test_metrics_b_night_uses_limit_start_reference_and_maker_share():
    night_ = {"entry_date": "2026-10-13", "method": "B", "pnl": 1.0}
    f = lambda leg, side, px, liq, ts: {**fill(leg, side, 1.0, px, 0.0 if liq == "MAKER" else 0.04, liq, ts),
                                         "client_id": f"on-20261013-{leg}0"}
    fills = [f("e", "BUY", 100.0, "MAKER", kst("2026-10-13", "15:30:20")),
             f("x", "SELL", 101.0, "TAKER", kst("2026-10-14", "08:59:30"))]
    ref = refs({kst("2026-10-13", "15:30"): 100.01, kst("2026-10-14", "08:57"): 101.02,
                kst("2026-10-14", "09:00"): 101.0})
    m = night_metrics(night_, fills, {}, SPEC, ref)
    assert m["ref_exit"] == 101.02                                       # B 청산 기준 = 08:57
    assert m["slip_entry_bp"] < 0                                        # maker 로 기준가보다 싸게 삼
    assert m["maker_entry"] == 1.0 and m["maker_exit"] == 0.0
    assert m["on_time"]


def test_metrics_stop_night_has_no_exit_slippage():
    night_ = {"entry_date": "2026-10-12", "method": "A", "pnl": -12.0}
    fills = [fill("e", "BUY", 1.0, 100.0, 0.04, "TAKER", kst("2026-10-12", "15:30:01")),
             fill("s", "SELL", 1.0, 88.0, 0.035, "TAKER", kst("2026-10-12", "20:00"))]
    m = night_metrics(night_, fills, {}, SPEC, lambda t: 100.0)
    assert m["how"] == "손절" and m["slip_exit_bp"] is None and m["cost_rt_bp"] is None and m["on_time"]


def test_late_entry_is_not_on_time():
    night_ = {"entry_date": "2026-10-12", "method": "A", "pnl": 0.0}
    fills = [fill("e", "BUY", 1.0, 100.0, 0.04, "TAKER", kst("2026-10-12", "15:31")),
             fill("x", "SELL", 1.0, 100.0, 0.04, "TAKER", kst("2026-10-13", "08:59:01"))]
    assert not night_metrics(night_, fills, {}, SPEC, lambda t: 100.0)["on_time"]


def test_summarize_checks_against_judgement():
    base = {"how": "청산", "on_time": True, "pnl": 0.1, "maker_entry": 1.0, "maker_exit": 0.5}
    rows = [{**base, "method": "A", "slip_entry_bp": 1.0, "slip_exit_bp": 2.0, "tracking_bp": 5.0, "cost_rt_bp": 11.0},
            {**base, "method": "A", "slip_entry_bp": 1.5, "slip_exit_bp": 0.5, "tracking_bp": -3.0, "cost_rt_bp": 10.0},
            {**base, "method": "B", "slip_entry_bp": -2.0, "slip_exit_bp": 3.0, "tracking_bp": 1.0, "cost_rt_bp": 5.0}]
    s = summarize(rows, SPEC)
    c = s["checks"]
    assert c["A 슬리피지 평균 bp"]["value"] == pytest.approx(1.25) and c["A 슬리피지 평균 bp"]["ok"]
    assert c["B 왕복 비용 평균 bp"]["value"] == 5.0 and c["B 왕복 비용 평균 bp"]["ok"] is False
    assert c["정시 비율"]["ok"] and s["b_maker_share"] == 0.75
    assert "❌ B 왕복 비용" in summary_text("live", [], s)


def test_summarize_empty_is_pending():
    s = summarize([], SPEC)
    assert all(c["ok"] is None for c in s["checks"].values())
    assert "⏳" in summary_text("live", [], s)


# ---------------------------------------------------------------- 적재 (autocoin_test)
@pytest.fixture(scope="module")
def conn():
    try:
        db.ensure_database(TEST_DSN)
        db.apply_schema(TEST_DSN)
    except psycopg.OperationalError as e:
        pytest.skip(f"PostgreSQL 사용 불가: {e}")
    with psycopg.connect(TEST_DSN, autocommit=True) as c:
        for t in ("events", "fills", "decisions", "nights", "night_metrics"):
            c.execute(f"DELETE FROM exec.{t} WHERE mode = 'paper'")
        yield c


def test_ingest_simulated_night_and_compute(tmp_path, conn):
    r, _, _, _ = make(tmp_path / "paper", kst("2026-10-12", "15:00"),
                      lambda t: 100.0 if t < kst("2026-10-13", "08:00") else 101.0)
    r.run_night(night("2026-10-12"))
    n1 = ingest("paper", conn, tmp_path)
    assert n1 > 5 and ingest("paper", conn, tmp_path) == 0             # 멱등
    assert conn.execute("SELECT count(*) FROM exec.fills WHERE mode='paper'").fetchone()[0] == 2
    assert conn.execute("SELECT count(*) FROM exec.decisions WHERE mode='paper'").fetchone()[0] == 2
    # 시뮬 가격은 기준가 = 경로 가격(매수 호가)
    path_ref = lambda t: 100.0 if t < kst("2026-10-13", "08:00") else 101.0
    ms = compute("paper", conn, ref=path_ref)
    m = ms[0]
    assert m["slip_entry_bp"] == pytest.approx(1.0)                    # 매도호가 100.01 에 삼 = 1bp
    assert m["slip_exit_bp"] == pytest.approx(0.0) and m["on_time"]
    assert load_metrics("paper", conn)[0]["method"] == "A"
    assert "2026-10-12 [A] 청산" in night_line(m)
