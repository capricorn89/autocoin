"""복제 엔진(진입/만기/청산/τ/리밸런스) 테스트 — 0DTE 모델.

KST(UTC+9) 기준: 진입 08:00 KST = 전일 23:00 UTC, 만기 15:30 KST = 당일 06:30 UTC.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.config import Config
from legacy.straddle.strategy import StraddleReplicator


def _cfg(**kw) -> Config:
    c = Config(**kw)
    c.validate()
    return c


def _utc(y, m, d, h, mi=0):
    return datetime(y, m, d, h, mi, tzinfo=timezone.utc)


def test_entry_at_strike_time_kst():
    # 08:00 KST(Apr2) = 23:00 UTC(Apr1) 에 진입, 그 가격이 K
    eng = StraddleReplicator(_cfg())
    d = eng.step(_utc(2026, 4, 1, 23, 0), price=150.0, sigma=0.5)
    assert d.new_cycle and d.active and d.K == 150.0


def test_active_during_window():
    eng = StraddleReplicator(_cfg())
    eng.step(_utc(2026, 4, 1, 23, 0), 150.0, 0.5)              # 진입
    d = eng.step(_utc(2026, 4, 2, 2, 0), 155.0, 0.5)          # 11:00 KST, 장중
    assert d.active and not d.new_cycle and d.K == 150.0


def test_settle_and_flat_after_expiry():
    eng = StraddleReplicator(_cfg())
    eng.step(_utc(2026, 4, 1, 23, 0), 150.0, 0.5)             # 진입
    # 15:30 KST = 06:30 UTC 만기. 그 시점에 정산 + 청산
    d = eng.step(_utc(2026, 4, 2, 6, 30), 160.0, 0.5)
    assert d.settled and not d.active
    assert d.prev_K == 150.0
    assert d.target_position == 0.0
    # 만기 이후~다음 진입 전: flat 유지
    d2 = eng.step(_utc(2026, 4, 2, 10, 0), 162.0, 0.5)
    assert not d2.active and not d2.settled and d2.target_position == 0.0


def test_flat_before_first_strike():
    eng = StraddleReplicator(_cfg())
    # 05:00 KST(진입 전) = 20:00 UTC 전일 → flat, 보유 없음
    d = eng.step(_utc(2026, 4, 1, 20, 0), 150.0, 0.5)
    assert not d.active and not d.new_cycle and d.target_position == 0.0


def test_new_cycle_next_day():
    eng = StraddleReplicator(_cfg())
    eng.step(_utc(2026, 4, 1, 23, 0), 150.0, 0.5)             # day1 진입
    eng.step(_utc(2026, 4, 2, 6, 30), 160.0, 0.5)            # day1 만기/청산
    d = eng.step(_utc(2026, 4, 2, 23, 0), 170.0, 0.5)        # day2 진입
    assert d.new_cycle and d.active and d.K == 170.0


def test_tau_decays_to_floor():
    cfg = _cfg()
    eng = StraddleReplicator(cfg)
    eng.step(_utc(2026, 4, 1, 23, 0), 150.0, 0.5)
    t0 = eng.tau(_utc(2026, 4, 1, 23, 0))    # 진입(만기까지 7.5h)
    t1 = eng.tau(_utc(2026, 4, 2, 3, 0))     # 중간
    t2 = eng.tau(_utc(2026, 4, 2, 6, 25))    # 만기 직전
    assert t0 > t1 > t2 >= cfg.tau_floor


def test_rebalance_interval_respected():
    eng = StraddleReplicator(_cfg(rebalance_interval="1h"))
    eng.step(_utc(2026, 4, 1, 23, 0), 150.0, 0.5)            # 진입 -> reb True
    d_soon = eng.step(_utc(2026, 4, 1, 23, 30), 151.0, 0.5)  # 30분 -> False
    assert not d_soon.is_rebalance
    d_late = eng.step(_utc(2026, 4, 2, 0, 5), 152.0, 0.5)    # 1h+ -> True
    assert d_late.is_rebalance


def test_custom_strike_expiry_time_utc():
    cfg = _cfg(strike_time="09:00", expiry_time="16:00", strike_timezone="UTC")
    eng = StraddleReplicator(cfg)
    d = eng.step(_utc(2026, 4, 1, 9, 0), 200.0, 0.5)
    assert d.new_cycle and d.K == 200.0
    d2 = eng.step(_utc(2026, 4, 1, 16, 0), 205.0, 0.5)       # 만기 -> 청산
    assert d2.settled and not d2.active


def test_expiry_offset_overnight_hold():
    # offset=1: day1 08:00 진입 → day2 15:30 만기(~31.5h). 비중복 사이클.
    # day2 08:00(만기 전) 에는 롤하지 않고 기존 K 유지.
    # 만기 후 flat → day3 08:00 에 새 사이클 시작.
    cfg = _cfg(expiry_offset_days=1, strike_time="08:00", expiry_time="15:30",
               strike_timezone="UTC")
    eng = StraddleReplicator(cfg)
    eng.step(_utc(2026, 4, 1, 8, 0), 100.0, 0.5)                   # day1 진입, K=100
    d_night = eng.step(_utc(2026, 4, 1, 20, 0), 101.0, 0.5)        # 밤에도 활성
    assert d_night.active and d_night.K == 100.0
    # day2 08:00: 만기 전이므로 롤 없이 유지
    d_no_roll = eng.step(_utc(2026, 4, 2, 8, 0), 102.0, 0.5)
    assert d_no_roll.active and not d_no_roll.new_cycle and d_no_roll.K == 100.0
    # day2 15:30: 실제 만기 → 정산
    d_exp = eng.step(_utc(2026, 4, 2, 15, 30), 105.0, 0.5)
    assert d_exp.settled and not d_exp.active and d_exp.prev_K == 100.0
    # day2 18:00: flat 유지
    d_flat = eng.step(_utc(2026, 4, 2, 18, 0), 106.0, 0.5)
    assert not d_flat.active and not d_flat.settled
    # day3 08:00: 새 사이클 진입
    d_new = eng.step(_utc(2026, 4, 3, 8, 0), 107.0, 0.5)
    assert d_new.new_cycle and d_new.active and d_new.K == 107.0
