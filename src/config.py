"""설정 로드/검증 (config.yaml -> dataclass)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import time
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

# rebalance_interval / backtest.interval 문자열 -> 초
_UNIT_SECONDS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_interval(text: str) -> int:
    """'5m', '1h', '30s' -> 초 단위 정수."""
    m = re.fullmatch(r"\s*(\d+)\s*([smhd])\s*", str(text).lower())
    if not m:
        raise ValueError(f"interval 형식 오류: {text!r} (예: '1m', '5m', '1h')")
    return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]


def parse_hhmm(text: str) -> time:
    m = re.fullmatch(r"\s*(\d{1,2}):(\d{2})\s*", str(text))
    if not m:
        raise ValueError(f"strike_time 형식 오류: {text!r} (예: '08:00')")
    h, mi = int(m.group(1)), int(m.group(2))
    if not (0 <= h < 24 and 0 <= mi < 60):
        raise ValueError(f"strike_time 범위 오류: {text!r}")
    return time(hour=h, minute=mi)


@dataclass
class ExecutionConfig:
    mode: str = "paper"
    poll_seconds: int = 15
    state_file: str = "results/live_state.json"


@dataclass
class BacktestConfig:
    interval: str = "1m"
    start: str | None = None
    end: str | None = None


@dataclass
class Config:
    symbol: str = "EWYUSDT"
    strike_time: str = "08:00"
    expiry_time: str = "15:30"
    expiry_offset_days: int = 0
    strike_timezone: str = "Asia/Seoul"
    rebalance_interval: str = "5m"
    rebalance_band: float = 0.0
    vol_mode: str = "realized"
    vol_value: float = 0.6
    vol_window: int = 20
    risk_free_rate: float = 0.0
    contracts: float = 1.0
    tau_floor: float = 1.0 / (365 * 24)
    taker_fee_bps: float = 5.0
    maker_fee_bps: float = 2.0
    slippage_bps: float = 1.0
    backtest: BacktestConfig = field(default_factory=BacktestConfig)
    execution: ExecutionConfig = field(default_factory=ExecutionConfig)

    # --- 파생값 (검증/편의) ---
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.strike_timezone)

    @property
    def strike_t(self) -> time:
        return parse_hhmm(self.strike_time)

    @property
    def expiry_t(self) -> time:
        return parse_hhmm(self.expiry_time)

    @property
    def rebalance_seconds(self) -> int:
        return parse_interval(self.rebalance_interval)

    def validate(self) -> None:
        assert self.expiry_offset_days >= 0, "expiry_offset_days >= 0"
        # 당일 만기(offset=0)면 만기 시각이 진입 시각보다 늦어야 함
        if self.expiry_offset_days == 0:
            assert parse_hhmm(self.expiry_time) > parse_hhmm(self.strike_time), \
                "expiry_offset_days=0 이면 expiry_time > strike_time 이어야 함"
        assert self.vol_mode in ("realized", "fixed"), "vol_mode: realized|fixed"
        assert self.vol_value > 0, "vol_value > 0"
        assert self.vol_window >= 2, "vol_window >= 2"
        assert self.contracts > 0, "contracts > 0"
        assert self.tau_floor > 0, "tau_floor > 0"
        assert self.execution.mode in ("paper", "live"), "execution.mode: paper|live"
        parse_hhmm(self.strike_time)
        parse_hhmm(self.expiry_time)
        parse_interval(self.rebalance_interval)
        parse_interval(self.backtest.interval)
        ZoneInfo(self.strike_timezone)


def load_config(path: str | Path = "config.yaml") -> Config:
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    bt = BacktestConfig(**(raw.pop("backtest", {}) or {}))
    ex = ExecutionConfig(**(raw.pop("execution", {}) or {}))
    cfg = Config(backtest=bt, execution=ex, **raw)
    cfg.validate()
    return cfg
