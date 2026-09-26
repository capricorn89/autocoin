"""수집 중 잠자기 방지 정책 (macOS).

정책 (사용자 결정 2026-09-27)
 - AC 전원: 항상 잠자기 방지
 - 배터리: 잔량 battery_floor(기본 30%) 이상 + 네트워크 연결일 때만 잠자기 방지
   - 잔량이 기준 미만이면 해제 → 시스템 설정대로 잠든다 (이 맥은 배터리 1분 유휴 시 잠자기)
   - 네트워크 끊김이 net_grace_s(기본 5분) 넘게 이어지면 해제. 일시적인 DNS·Wi-Fi 끊김으로
     바로 잠들면 재연결 기회까지 잃으므로 유예를 둔다.
 - caffeinate -i -s -w <pid>: 유휴 잠자기를 막는다(-s 는 AC 에서만 유효). 화면 꺼짐은 막지 않고,
   덮개를 닫으면(외부 모니터 없을 때) 잠든다. 수집기가 죽으면 -w 로 함께 끝난다.
 - 시스템 전원 설정(pmset)은 바꾸지 않는다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import re
import subprocess
import time
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger(__name__)

CAFFEINATE = "/usr/bin/caffeinate"


@dataclass(frozen=True)
class PowerState:
    on_ac: bool
    battery_pct: int | None   # 배터리 없는 기기면 None


def parse_pmset_batt(text: str) -> PowerState:
    """`pmset -g batt` 출력 파싱."""
    src = re.search(r"drawing from '([^']+)'", text)
    pct = re.search(r"(\d+)%", text)
    on_ac = src is None or "AC" in src.group(1)
    return PowerState(on_ac=on_ac, battery_pct=int(pct.group(1)) if pct else None)


def read_power(run: Callable = subprocess.run) -> PowerState:
    r = run(["/usr/bin/pmset", "-g", "batt"], capture_output=True, text=True, timeout=5)
    return parse_pmset_batt(r.stdout)


def network_up(run: Callable = subprocess.run) -> bool:
    """기본 경로(default route)가 있으면 연결로 본다. DNS 실패까지는 판단하지 않는다."""
    r = run(["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, timeout=5)
    return r.returncode == 0 and "interface:" in r.stdout


@dataclass(frozen=True)
class SleepPolicy:
    battery_floor: int = 30
    net_grace_s: float = 300.0

    def should_hold(self, power: PowerState, net_ok: bool,
                    net_down_for_s: float) -> tuple[bool, str, str]:
        """(잠자기 방지 여부, 상태 코드, 사람이 읽는 사유). 상태 코드가 바뀔 때만 기록한다."""
        if power.on_ac:
            return True, "ac", "AC 전원"
        pct = power.battery_pct
        if pct is not None and pct < self.battery_floor:
            return False, "battery_low", f"배터리 {pct}% < {self.battery_floor}%"
        if not net_ok and net_down_for_s >= self.net_grace_s:
            return False, "net_down", (f"배터리 {pct}%, 네트워크 끊김 {net_down_for_s:.0f}s "
                                       f"≥ {self.net_grace_s:.0f}s")
        if not net_ok:
            return True, "net_grace", f"배터리 {pct}%, 네트워크 끊김 유예 중 ({net_down_for_s:.0f}s)"
        return True, "battery_ok", f"배터리 {pct}% ≥ {self.battery_floor}%, 네트워크 연결"


def _spawn_caffeinate() -> subprocess.Popen:
    return subprocess.Popen([CAFFEINATE, "-i", "-s", "-w", str(os.getpid())])


class SleepGuard:
    """주기적으로 전원·네트워크를 확인해 caffeinate 를 켜고 끈다."""

    def __init__(self, policy: SleepPolicy = SleepPolicy(),
                 emit: Callable[[dict], None] | None = None,
                 read_power: Callable[[], PowerState] = read_power,
                 network_up: Callable[[], bool] = network_up,
                 spawn: Callable[[], subprocess.Popen] = _spawn_caffeinate,
                 clock: Callable[[], float] = time.monotonic, interval: float = 30.0):
        self.policy = policy
        self._emit = emit
        self._read_power = read_power
        self._network_up = network_up
        self._spawn = spawn
        self._clock = clock
        self.interval = interval
        self._proc: subprocess.Popen | None = None
        self._net_down_since: float | None = None
        self._last_code: str | None = None

    @property
    def holding(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def tick(self) -> bool:
        now = self._clock()
        try:
            power = self._read_power()
            net_ok = self._network_up()
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("전원/네트워크 상태 확인 실패 — 현재 상태 유지: %s", e)
            return self.holding

        if net_ok:
            self._net_down_since = None
        elif self._net_down_since is None:
            self._net_down_since = now
        down_for = 0.0 if net_ok else now - self._net_down_since

        hold, code, reason = self.policy.should_hold(power, net_ok, down_for)
        if hold and not self.holding:
            if self._proc is not None:
                log.warning("caffeinate 가 예기치 않게 종료됨 — 다시 실행")
            self._proc = self._spawn()
        elif not hold and self.holding:
            self.release()

        if code != self._last_code:
            log.info("잠자기 방지 %s — %s", "켜짐" if hold else "해제", reason)
            if self._emit is not None:
                self._emit({"kind": "power", "recv_ts": int(time.time() * 1000), "hold": hold,
                            "code": code, "reason": reason, "on_ac": power.on_ac,
                            "battery_pct": power.battery_pct, "net_ok": net_ok})
        self._last_code = code
        return hold

    def release(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
        self._proc = None

    async def run(self, stop: asyncio.Event) -> None:
        try:
            while not stop.is_set():
                self.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=self.interval)
                except TimeoutError:
                    pass
        finally:
            self.release()
