"""스펙 로드 + 엔진 파라미터 + 수량 규칙.

스펙 파일은 동결본이다 (지금 실행은 spec_v3.yaml). 여기서는 읽고 계산만 한다.

    python -m src.strategies.pastofi_crypto.spec            # 스펙 요약 + 지금 가격 기준 수량
"""
from __future__ import annotations

import dataclasses
import hashlib
import math
from dataclasses import dataclass
from pathlib import Path

import yaml

from .engine import LOGIC1, SymbolEngine
from .params import DEFAULT, Params

SPEC_V1 = Path(__file__).with_name("spec_v1.yaml")
SPEC_V2 = Path(__file__).with_name("spec_v2.yaml")
SPEC_PATH = Path(__file__).with_name("spec_v3.yaml")     # 지금 실행 스펙


class SpecError(RuntimeError):
    """스펙과 맞지 않아 실행하면 안 되는 상태."""


@dataclass(frozen=True)
class Spec:
    raw: dict
    sha256: str

    @classmethod
    def load(cls, path: Path = SPEC_PATH) -> Spec:
        data = Path(path).read_bytes()
        return cls(raw=yaml.safe_load(data), sha256=hashlib.sha256(data).hexdigest())

    @property
    def version(self) -> str:
        return self.raw["version"]

    @property
    def symbols(self) -> list[str]:
        return list(self.raw["symbols"])

    def params(self) -> Params:
        """엔진 Params. 스펙에 있는 값을 전부 명시해서 덮어쓴다 (빠진 필드가 있으면 거부)."""
        p = dict(self.raw["engine"]["params"])
        missing = {f.name for f in dataclasses.fields(Params)} - set(p)
        if missing:
            raise SpecError(f"스펙 engine.params 에 빠진 값: {sorted(missing)} — 기본값(KRX)을 쓰지 않는다")
        typed = {f.name: type(getattr(DEFAULT, f.name))(p[f.name]) for f in dataclasses.fields(Params)}
        return dataclasses.replace(DEFAULT, **typed)

    def new_engine(self, first_mid: float) -> SymbolEngine:
        if self.raw["engine"]["fut_base"] != "first_mid":
            raise SpecError("fut_base 는 first_mid 만 지원한다")
        return SymbolEngine(self.params(), fut_base=float(first_mid), market=self.raw["engine"]["market"])

    @property
    def logic(self) -> str:
        if self.raw["logics"] != [LOGIC1]:
            raise SpecError(f"logics 는 [{LOGIC1}] 만 지원한다: {self.raw['logics']}")
        return LOGIC1

    @property
    def qty_scale(self) -> float:
        return float(self.raw["input"]["qty_scale"])

    @property
    def sizing_rule(self) -> str:
        """min_notional (v1: 종목마다 최소 명목) · equal_notional (v2: 4종목 명목을 가장 큰 최소 명목에 맞춘다)."""
        rule = self.raw["sizing"].get("rule", "min_notional")
        if rule not in ("min_notional", "equal_notional"):
            raise SpecError(f"알 수 없는 sizing.rule: {rule}")
        return rule

    @property
    def maker_exit_s(self) -> float | None:
        """마감 몇 초 전에 bid1 메이커 청산을 거나 (v3). 없으면 None — v1·v2 는 시장가 청산만."""
        m = (self.raw.get("exit") or {}).get("maker")
        if not m:
            return None
        if m.get("price") != "bid1":
            raise SpecError(f"exit.maker.price 는 bid1 만 지원한다: {m.get('price')}")
        s = float(m["before_s"])
        if not 0 < s < self.params().hold:
            raise SpecError(f"exit.maker.before_s 는 0 초과 hold 미만이어야 한다: {s}")
        return s


# ---------------------------------------------------------------- 수량
@dataclass(frozen=True)
class SymbolFilters:
    symbol: str
    step: float
    min_qty: float
    min_notional: float
    tick: float

    @classmethod
    def from_exchange_info(cls, info: dict, symbol: str) -> SymbolFilters:
        s = next((x for x in info["symbols"] if x["symbol"] == symbol), None)
        if s is None:
            raise SpecError(f"exchangeInfo 에 {symbol} 이 없다")
        f = {x["filterType"]: x for x in s["filters"]}
        return cls(symbol, float(f["LOT_SIZE"]["stepSize"]), float(f["LOT_SIZE"]["minQty"]),
                   float(f["MIN_NOTIONAL"]["notional"]), float(f["PRICE_FILTER"]["tickSize"]))


def _decimals(step: float) -> int:
    return max(0, -int(math.floor(math.log10(step) + 1e-9)))


def order_qty(price: float, flt: SymbolFilters, mult: float) -> float:
    """명목 ≥ MIN_NOTIONAL × mult 를 만족하는 가장 작은 stepSize 배수 (minQty 이상)."""
    if not price > 0:
        raise SpecError(f"{flt.symbol} 가격이 잘못됐다: {price}")
    need = flt.min_notional * mult / price
    n = max(math.ceil(need / flt.step - 1e-9), math.ceil(flt.min_qty / flt.step - 1e-9), 1)
    q = round(n * flt.step, _decimals(flt.step))
    while q * price < flt.min_notional * mult:          # 부동소수 경계 보정
        n += 1
        q = round(n * flt.step, _decimals(flt.step))
    return q


def equal_notional_qty(symbol: str, prices: dict[str, float], filters: dict[str, SymbolFilters],
                       mult: float) -> float:
    """목표 명목(종목별 최소 수량 명목 중 최대)에 가장 가까운 stepSize 배수. 자기 최소 수량 아래로는 내려가지 않는다."""
    target = max(order_qty(prices[s], filters[s], mult) * prices[s] for s in prices)
    flt, px = filters[symbol], prices[symbol]
    floor_q = order_qty(px, flt, mult)
    q = round(round(target / px / flt.step) * flt.step, _decimals(flt.step))
    return max(q, floor_q)


def sized(spec: Spec, symbol: str, prices: dict[str, float], filters: dict[str, SymbolFilters]) -> float:
    """수량 규칙 + max_qty 상한. prices 는 종목별 가격 (equal_notional 은 스펙 종목 전부 필요). 상한을 넘으면 SpecError."""
    mult = float(spec.raw["sizing"]["min_notional_mult"])
    if spec.sizing_rule == "equal_notional":
        missing = [s for s in spec.symbols if not prices.get(s)]
        if missing:
            raise SpecError(f"{symbol} 수량 계산에 필요한 가격이 없다: {', '.join(missing)}")
        q = equal_notional_qty(symbol, {s: prices[s] for s in spec.symbols}, filters, mult)
    else:
        q = order_qty(prices[symbol], filters[symbol], mult)
    cap = float(spec.raw["sizing"]["max_qty"][symbol])
    if q > cap + 1e-12:
        raise SpecError(f"{symbol} 수량 {q} 가 스펙 상한 {cap} 를 넘는다 (가격 {prices[symbol]}) — 실행 거부")
    return q


def main() -> None:
    import requests
    spec = Spec.load()
    print(f"{spec.version}  sha256={spec.sha256[:12]}  종목 {', '.join(spec.symbols)}")
    print("엔진:", spec.params())
    info = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15).json()
    filters = {s: SymbolFilters.from_exchange_info(info, s) for s in spec.symbols}
    prices = {s: float(requests.get("https://fapi.binance.com/fapi/v1/ticker/bookTicker",
                                    params={"symbol": s}, timeout=10).json()["bidPrice"]) for s in spec.symbols}
    print(f"수량 규칙: {spec.sizing_rule}")
    for sym in spec.symbols:
        flt, px = filters[sym], prices[sym]
        q = sized(spec, sym, prices, filters)
        print(f"{sym}: step {flt.step:g} · MIN_NOTIONAL ${flt.min_notional:g} · 가격 {px:g} → 수량 {q:g} (명목 ${q * px:.2f})")


if __name__ == "__main__":
    main()
