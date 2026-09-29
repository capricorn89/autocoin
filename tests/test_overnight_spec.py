"""라이브 스펙 v1 동결·밤 일정 계산 (WOO-96)."""
import copy
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest

from src.overnight.spec import Spec, SpecError

KST = ZoneInfo("Asia/Seoul")
# spec_v1.yaml 동결 해시. 이 값이 깨지면 v1 을 고친 것이다 → spec_v2.yaml 로 새로 만들 것
FROZEN_SHA256 = "3f870d0a85a0"


@pytest.fixture(scope="module")
def spec():
    return Spec.load()


def kst(d, hm):
    return datetime.fromisoformat(f"{d} {hm}").replace(tzinfo=KST)


def test_spec_v1_is_frozen(spec):
    assert spec.sha256.startswith(FROZEN_SHA256)
    assert spec.raw["version"] == 1


def test_account_expectations_match_woo100(spec):
    acc = spec.raw["account"]
    assert (acc["margin_type"], acc["leverage"], acc["multi_assets_margin"], acc["dual_side_position"]) == \
        ("ISOLATED", 1, False, False)
    assert spec.raw["qty"] == spec.raw["risk"]["max_abs_position"] == 1.0


def test_holidays_and_weekends_are_not_sessions(spec):
    for d in ("2026-10-05", "2026-10-09", "2026-12-25", "2026-12-31", "2027-01-01", "2026-10-03"):
        assert not spec.is_session(date.fromisoformat(d))
    assert spec.is_session(date(2026, 10, 6))


def test_outside_calendar_coverage_is_refused(spec):
    with pytest.raises(SpecError):
        spec.is_session(date(2027, 2, 1))
    with pytest.raises(SpecError):
        spec.is_session(date(2026, 9, 29))


def test_long_holiday_nights_are_skipped(spec):
    skipped = {n.entry_date.isoformat() for n in spec.nights() if n.skip and n.skip.startswith("연휴")}
    assert skipped == {"2026-10-02", "2026-10-08", "2026-12-24", "2026-12-30"}


def test_regular_weekend_is_held(spec):
    n = spec.night_for_entry(date(2026, 10, 16))
    assert n.exit_date == date(2026, 10, 19) and n.calendar_days == 3 and n.skip is None


def test_suneung_session_hours(spec):
    before = spec.night_for_entry(date(2026, 11, 18))
    assert before.exit_open == kst("2026-11-19", "10:00")
    t = spec.order_times(before)
    assert t["exit_flat_deadline"] == kst("2026-11-19", "09:59:50")
    after = spec.night_for_entry(date(2026, 11, 19))
    assert after.entry_close == kst("2026-11-19", "16:30")


def test_dividend_date_required_from_december(spec):
    n = spec.night_for_entry(date(2026, 12, 1))
    assert n.skip == "배당락일 미입력" and n.method is None
    with pytest.raises(SpecError):
        spec.order_times(n)


def test_ex_dividend_skips_night_before_and_after():
    raw = copy.deepcopy(Spec.load().raw)
    raw["nights"]["ewy_ex_dividend_dates"] = ["2026-12-16"]
    s = Spec(raw=raw, sha256="test")
    skip = {n.entry_date.isoformat(): n.skip for n in s.nights()}
    assert skip["2026-12-15"] == skip["2026-12-16"] == "EWY 배당락"
    assert skip["2026-12-14"] is None and skip["2026-12-17"] is None


def test_ab_assignment_is_pair_balanced(spec):
    m = [n.method for n in spec.nights() if n.skip is None]
    for i in range(0, len(m) - 1, 2):
        assert {m[i], m[i + 1]} == {"A", "B"}
    assert abs(m.count("A") - m.count("B")) <= 1
    assert m == [n.method for n in Spec.load().nights() if n.skip is None]   # 재현 가능


def test_order_times_by_method(spec):
    nights = [n for n in spec.nights() if n.skip is None and n.entry_date < date(2026, 11, 1)]
    a = next(n for n in nights if n.method == "A")
    b = next(n for n in nights if n.method == "B")
    ta, tb = spec.order_times(a), spec.order_times(b)
    assert ta["entry_market"] == kst(a.entry_date, "15:30") and "entry_limit" not in ta
    assert ta["exit_market"] == kst(a.exit_date, "08:59")
    assert tb["entry_limit"] == kst(b.entry_date, "15:30") and tb["entry_market"] == kst(b.entry_date, "15:31:30")
    assert tb["exit_limit"] == kst(b.exit_date, "08:57") and tb["exit_market"] == kst(b.exit_date, "08:59:30")
    assert tb["exit_flat_deadline"] == kst(b.exit_date, "08:59:50")
    assert ta["entry_give_up"] == kst(a.entry_date, "15:40")
    assert ta["residual_check"] == kst(a.exit_date, "09:05")
