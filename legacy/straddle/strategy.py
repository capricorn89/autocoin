"""스트래들 복제 엔진 (백테스트·라이브 공용, 단일 소스).

책임:
 - 사이클(진입~만기) 판정 및 행사가 K 고정/리셋
 - 잔존기간 τ 계산 (만기 시각까지 0으로 감쇠)
 - 활성 구간 [strike_time, expiry_time] 밖에서는 청산(target=0)
 - 스트래들 델타 -> 목표 선물 포지션 산출
 - 리밸런스 시점 판정 (rebalance_interval)

사이클 모델 (expiry_offset_days 로 만기 폭 설정):
 - 매일 strike_time(예: 08:00 KST)에 그 시점 가격을 K로 진입
 - 만기 = 진입일 + expiry_offset_days 일의 expiry_time(예: 15:30 KST)
 - 0DTE(offset=0): 진입 당일 15:30 만기. 매일 새 사이클.
 - 1DTE(offset=1): 진입 다음날 15:30 만기(약 31.5h). 사이클이 만기 전
   다음 strike_time을 지나도 조기 롤하지 않고 실제 만기까지 보유.
   만기 후 다음 strike_time(08:00)까지 flat → 비중복 사이클.
 - 만기 후 ~ 다음 진입 전까지는 포지션 없음(flat)

포지션/현금 등 포트폴리오 회계는 브로커/드라이버가 담당한다(관심사 분리).
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from src.config import Config
from .straddle import straddle_delta, straddle_price

YEAR_SECONDS = 365.0 * 24 * 3600


@dataclass
class Decision:
    """한 틱의 엔진 판정 결과."""
    ts: datetime
    price: float
    active: bool             # 활성 구간(스트래들 보유) 여부
    new_cycle: bool          # 이 틱에서 신규 진입(K 설정) 발생 여부
    settled: bool            # 이 틱에서 만기/롤로 직전 사이클 정산 발생 여부
    K: float | None          # 현재 사이클 행사가 (flat이면 None)
    tau: float               # 잔존기간(연)
    sigma: float             # 사용된 변동성
    delta: float             # 스트래들 델타 (2N(d1)-1)
    target_position: float   # 목표 선물 수량 (delta*contracts, flat이면 0)
    is_rebalance: bool       # 이 틱에 리밸런스(매매 검토) 여부
    prev_K: float | None = None   # 정산된 직전 사이클 K (settled일 때)


class StraddleReplicator:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.K: float | None = None
        self.anchor: datetime | None = None   # 현재 사이클 진입(UTC aware)
        self.expiry: datetime | None = None    # 현재 사이클 만기(UTC aware)
        self.active: bool = False
        self._last_rebalance: datetime | None = None

    # ---- 활성 사이클(진입/만기) 판정 ----
    def active_window(self, ts: datetime) -> tuple[datetime, datetime] | None:
        """ts 가 속한 활성 사이클의 (anchor, expiry)를 UTC aware 로 반환.

        활성 구간이 아니면 None(flat). 매일 strike_time 에 재진입(daily re-strike)을
        가정하므로 사이클은 겹치지 않는다(offset>=1 이면 다음날 진입이 롤 역할).
        """
        tz = self.cfg.tz
        st, et = self.cfg.strike_t, self.cfg.expiry_t
        local = ts.astimezone(tz)

        today_anchor = local.replace(hour=st.hour, minute=st.minute, second=0, microsecond=0)
        if local >= today_anchor:
            anchor_local = today_anchor
        else:
            anchor_local = today_anchor - timedelta(days=1)

        expiry_date = anchor_local.date() + timedelta(days=self.cfg.expiry_offset_days)
        expiry_local = datetime.combine(expiry_date, et, tzinfo=tz)

        # 진입 포함, 만기 미포함(만기 시각에 정산)
        if anchor_local <= local < expiry_local:
            return anchor_local.astimezone(timezone.utc), expiry_local.astimezone(timezone.utc)
        return None

    def seed(self, anchor: datetime, expiry: datetime, K: float) -> None:
        """라이브 중도 시작/재시작 시 현재 사이클 상태를 주입.

        이후 첫 step 에서 anchor 가 일치하므로 신규진입(K 재설정)이 일어나지 않고
        주어진 K(예: 08:00 가격)로 이어간다.
        """
        self.anchor, self.expiry, self.K = anchor, expiry, K
        self.active = True
        self._last_rebalance = None  # 첫 틱에 즉시 리밸런스

    def tau(self, ts: datetime) -> float:
        """잔존기간(연). tau_floor 로 하한."""
        assert self.expiry is not None
        secs = (self.expiry - ts).total_seconds()
        return max(secs / YEAR_SECONDS, self.cfg.tau_floor)

    # ---- 메인 스텝 ----
    def step(self, ts: datetime, price: float, sigma: float) -> Decision:
        """한 틱 처리. ts 는 tz-aware(UTC 권장)."""
        new_cycle = False
        settled = False
        prev_K = None
        tau = 0.0
        delta = 0.0
        target = 0.0

        if self.active and self.expiry is not None and ts < self.expiry:
            # 현재 사이클이 아직 만기 전 — active_window 무시하고 유지.
            # offset > 0 일 때 만기 전 strike_time을 지나도 조기 롤되지 않는다.
            tau = self.tau(ts)
            delta = straddle_delta(price, self.K, tau, sigma, self.cfg.risk_free_rate)
            target = delta * self.cfg.contracts
        else:
            # 만기 도달(ts >= expiry)이면 먼저 강제 정산
            if self.active and self.expiry is not None:
                settled = True
                prev_K = self.K
                self.active = False

            # 새 사이클 판정: 앵커가 직전 만기 이후여야만 유효한 진입점
            win = self.active_window(ts)
            if win is not None:
                anchor, expiry = win
                # 직전 만기 이전에 시작된 앵커는 사이클 중복 → 무효
                # anchor == expiry 는 허용: strike_time == expiry_time 롤(당일 즉시 재진입)
                if self.expiry is not None and anchor < self.expiry:
                    win = None

            if win is not None:
                anchor, expiry = win
                if self.anchor != anchor:
                    if self.active:
                        settled = True
                        prev_K = self.K
                    self.anchor, self.expiry, self.K = anchor, expiry, price
                    self.active = True
                    new_cycle = True
                    self._last_rebalance = ts
                tau = self.tau(ts)
                delta = straddle_delta(price, self.K, tau, sigma, self.cfg.risk_free_rate)
                target = delta * self.cfg.contracts

        is_reb = new_cycle or settled or (self.active and self._is_rebalance_time(ts))
        if is_reb and self.active:
            self._last_rebalance = ts

        return Decision(
            ts=ts, price=price, active=self.active, new_cycle=new_cycle, settled=settled,
            K=self.K if self.active else None, tau=tau, sigma=sigma, delta=delta,
            target_position=target, is_rebalance=is_reb, prev_K=prev_K,
        )

    def _is_rebalance_time(self, ts: datetime) -> bool:
        if self._last_rebalance is None:
            return True
        return (ts - self._last_rebalance).total_seconds() >= self.cfg.rebalance_seconds

    # ---- 진입 시 이론 프리미엄(정산/리포트용) ----
    def entry_premium(self, S: float, sigma: float) -> float:
        """현재 사이클 진입(ATM) 시 이론 스트래들 프리미엄."""
        assert self.K is not None and self.expiry is not None and self.anchor is not None
        tau0 = max((self.expiry - self.anchor).total_seconds() / YEAR_SECONDS, self.cfg.tau_floor)
        return straddle_price(S, self.K, tau0, sigma, self.cfg.risk_free_rate) * self.cfg.contracts
