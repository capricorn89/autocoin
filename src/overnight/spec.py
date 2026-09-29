"""라이브 스펙 로드 + 밤 일정 계산 (WOO-96).

스케줄러(WOO-98)·리스크 가드·체결 로그가 모두 이 모듈로 같은 스펙을 읽는다.
스펙 파일은 동결본이라 여기서 값을 바꾸지 않는다. 계산만 한다.

    python -m src.overnight.spec            # 커버 구간 밤 일정 출력
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

SPEC_PATH = Path(__file__).with_name("spec_v1.yaml")
UTC = ZoneInfo("UTC")


class SpecError(RuntimeError):
    """스펙 범위 밖이거나 필수 입력 누락 — 실행하면 안 된다."""


@dataclass(frozen=True)
class Night:
    entry_date: date
    exit_date: date
    entry_close: datetime       # 진입일 장마감 (UTC)
    exit_open: datetime         # 청산일 개장 (UTC)
    skip: str | None            # 스킵 사유 (None 이면 거래)
    method: str | None          # "A" | "B". 스킵한 밤은 None

    @property
    def calendar_days(self) -> int:
        return (self.exit_date - self.entry_date).days


@dataclass(frozen=True)
class Spec:
    raw: dict
    sha256: str

    @classmethod
    def load(cls, path: Path = SPEC_PATH) -> Spec:
        data = path.read_bytes()
        return cls(raw=yaml.safe_load(data), sha256=hashlib.sha256(data).hexdigest())

    # ---- 달력
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.raw["calendar"]["tz"])

    @property
    def covers(self) -> tuple[date, date]:
        a, b = self.raw["calendar"]["covers"]
        return date.fromisoformat(a), date.fromisoformat(b)

    def _check_covered(self, d: date) -> None:
        a, b = self.covers
        if not a <= d <= b:
            raise SpecError(f"{d} 는 스펙 달력 범위({a}~{b}) 밖입니다. 휴장일 목록을 갱신하세요.")

    def is_session(self, d: date) -> bool:
        self._check_covered(d)
        return d.weekday() < 5 and d.isoformat() not in self.raw["calendar"]["holidays"]

    def sessions(self) -> list[date]:
        a, b = self.covers
        return [a + timedelta(days=i) for i in range((b - a).days + 1)
                if self.is_session(a + timedelta(days=i))]

    def session_times(self, d: date) -> tuple[datetime, datetime]:
        """(개장, 마감) UTC."""
        if not self.is_session(d):
            raise SpecError(f"{d} 는 KRX 거래일이 아닙니다.")
        cal = self.raw["calendar"]
        sp = cal["special_sessions"].get(d.isoformat(), {})
        o = time.fromisoformat(sp.get("open", cal["default_open"]))
        c = time.fromisoformat(sp.get("close", cal["default_close"]))
        return (datetime.combine(d, o, self.tz).astimezone(UTC),
                datetime.combine(d, c, self.tz).astimezone(UTC))

    # ---- 밤
    def _coin(self, d: date) -> int:
        return hashlib.sha256(f"{self.raw['nights']['ab_seed']}|{d.isoformat()}".encode()).digest()[0] % 2

    def _skip_reason(self, d0: date, d1: date) -> str | None:
        n = self.raw["nights"]
        if (d1 - d0).days >= n["skip_if_calendar_days_gte"]:
            return f"연휴 {(d1 - d0).days}일"
        exd = {date.fromisoformat(x) for x in n["ewy_ex_dividend_dates"]}
        if d0 in exd or d1 in exd:
            return "EWY 배당락"
        if not exd and d0 >= date.fromisoformat(n["require_ex_dividend_from"]):
            return "배당락일 미입력"
        return None

    def nights(self) -> list[Night]:
        """A/B 는 쌍 블록 무작위: 거래하는 밤을 연속 2개씩 묶고, 쌍 첫 진입일의 해시로 AB/BA 를 정한다.
        개수 차이는 항상 1 이하, 순서는 사전 예측 불가(시드 고정이라 재현은 가능)."""
        s = self.sessions()
        pairs = [(d0, d1, self._skip_reason(d0, d1)) for d0, d1 in zip(s[:-1], s[1:])]
        traded = [d0 for d0, _, skip in pairs if skip is None]
        method = {}
        for i, d0 in enumerate(traded):
            first = traded[i - i % 2]
            method[d0] = "AB"[(i % 2) ^ self._coin(first)]
        return [Night(entry_date=d0, exit_date=d1,
                      entry_close=self.session_times(d0)[1], exit_open=self.session_times(d1)[0],
                      skip=skip, method=method.get(d0))
                for d0, d1, skip in pairs]

    def night_for_entry(self, d: date) -> Night:
        for n in self.nights():
            if n.entry_date == d:
                return n
        raise SpecError(f"{d} 진입 밤이 없습니다 (비거래일이거나 다음 거래일이 범위 밖).")

    # ---- 주문 시각
    def order_times(self, night: Night) -> dict[str, datetime]:
        if night.method is None:
            raise SpecError(f"{night.entry_date} 밤은 스킵 대상입니다: {night.skip}")
        o = self.raw["orders"]
        m = o[night.method]
        at = lambda base, sec: base + timedelta(seconds=sec)
        t = {"entry_market": at(night.entry_close, m["entry_market_at"]),
             "exit_market": at(night.exit_open, m["exit_market_at"]),
             "entry_give_up": at(night.entry_close, o["entry_give_up_at"]),
             "exit_flat_deadline": at(night.exit_open, o["exit_flat_deadline"]),
             "residual_check": at(night.exit_open, self.raw["risk"]["residual_check_after_open_s"])}
        if night.method == "B":
            t["entry_limit"] = at(night.entry_close, m["entry_limit_from"])
            t["exit_limit"] = at(night.exit_open, m["exit_limit_from"])
        return t


def main() -> None:
    spec = Spec.load()
    kst = spec.tz
    print(f"spec v{spec.raw['version']} sha256={spec.sha256[:12]}")
    for n in spec.nights():
        if n.skip:
            print(f"{n.entry_date} → {n.exit_date}  {n.calendar_days}일  스킵: {n.skip}")
            continue
        t = spec.order_times(n)
        e, x = t.get("entry_limit", t["entry_market"]), t.get("exit_limit", t["exit_market"])
        print(f"{n.entry_date} {e.astimezone(kst):%H:%M:%S} → {n.exit_date} {x.astimezone(kst):%H:%M:%S} "
              f"(flat {t['exit_flat_deadline'].astimezone(kst):%H:%M:%S})  {n.calendar_days}일  {n.method}")


if __name__ == "__main__":
    main()
