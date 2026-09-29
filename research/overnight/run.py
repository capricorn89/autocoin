"""전체 재현 + 동결 수치 대조 (WOO-95).

    python -m research.overnight.run              # 재현·대조만
    python -m research.overnight.run --obsidian   # + 실험 노트·00-index 등록

EXPECTED 는 2026-09-29 분석에서 나온 값이다(데이터 종료 2026-09-28). 캐시(data/overnight/)가
같으면 허용오차 안에서 일치해야 한다. 어긋나면 종료코드 1.
"""
from __future__ import annotations

import argparse
import sys

import pandas as pd

from . import common as C, ewy, gaps, kodex

# 이름: (기대값, 허용오차)
EXPECTED = {
    "KOSPI200 오버나잇 CAGR": (0.223, 0.001),
    "SPY 오버나잇 CAGR": (0.088, 0.001),
    "EWYUSDT 한국 밤 총수익(비용0)": (1.584, 0.002),
    "EWYUSDT 한국 밤 총수익(taker)": (1.269, 0.002),
    "대체값 상관": (0.716, 0.002),
    "연결 CAGR(비용0)": (0.254, 0.002),
    "연결 CAGR(taker)": (-0.025, 0.002),
    "청산 정시 bp/밤": (77.58, 0.05),
    "청산 +1분 bp/밤": (69.31, 0.05),
    "최대역행": (-0.1137, 0.0005),
    "KODEX 밤 1.5bp CAGR": (0.102, 0.001),
    "KODEX 밤 1.5bp MDD": (-0.264, 0.001),
    "50/50 복합 CAGR": (0.084, 0.001),
    "50/50 복합 샤프": (0.888, 0.002),
}


def check(got: dict) -> pd.DataFrame:
    rows = []
    for k, (exp, tol) in EXPECTED.items():
        v = got.get(k)
        rows.append({"항목": k, "기대": exp, "재현": v,
                     "일치": v is not None and abs(v - exp) <= tol})
    return pd.DataFrame(rows)


def write_note(got: dict, table: pd.DataFrame) -> None:
    from src import obsidian
    meta = {"데이터구간_시작": C.START, "데이터구간_종료": C.END,
            "파라미터_KRX밤": f"{C.KRX_CLOSE}→{C.KRX_OPEN} KST", "파라미터_US밤": f"{C.US_CLOSE}→{C.US_OPEN} ET",
            "파라미터_수수료": "EWYUSDT maker 2bp/taker 5bp, KODEX 1.5bp, SPY 0.5bp (편도)",
            **{f"결과_{k}": round(float(v), 4) for k, v in got.items()},
            "태그": ["crypto-testbed", "experiment", "overnight"]}
    body = f"""## 목적

오버나잇 효과(한국 장 기준 밤 보유) 분석을 레포로 옮기고 결과를 재현한다. 라이브 스펙 동결(O0)의 근거.

## 재현 대조

{table.to_markdown(index=False)}

## 코드

`research/overnight/` — common · gaps · ewy · kodex · run. 산출물은 `results/overnight/`.

## 관련

- Linear 프로젝트: 오버나잇 효과 (KR)
- 다음: [[WOO-96]] 매매 규칙·리스크 한도 동결
"""
    note = obsidian.write_experiment("overnight-effect-reproduction", meta, body, issue_id="WOO-95")
    obsidian.register_experiment_in_index(note, "오버나잇 효과 분석 재현 (KOSPI200·EWYUSDT·KODEX)")
    print("Obsidian:", note)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--obsidian", action="store_true", help="실험 노트 기록")
    args = ap.parse_args(argv)
    got = {}
    for mod in (gaps, ewy, kodex):
        print(f"\n===== {mod.__name__} =====")
        got.update(mod.main())
    table = check(got)
    print("\n===== 재현 대조 =====")
    with pd.option_context("display.width", 200, "display.float_format", "{:,.4f}".format):
        print(table.to_string(index=False))
    ok = bool(table["일치"].all())
    if args.obsidian:
        write_note(got, table)
    print("산출물:", C.OUT_DIR)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
