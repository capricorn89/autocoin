"""Obsidian 볼트 작업 로그 기록: $OBSIDIAN_AUTOJI_PATH/crypto_testbed/

로그 유실을 뒤늦게 알아채지 않도록, 볼트 경로가 없으면 경고가 아니라 즉시 예외(hard fail)를 던진다.

규칙 (PLAN.md)
 - 실험 노트: 02-experiments/YYYY-MM-DD_<실험명>.md, frontmatter 에 실행시각·데이터구간·파라미터·커밋·결과 수치
 - 결정 노트: 채택안 + 기각한 대안과 기각 사유 필수
 - 제목에 Linear 이슈 ID: "[CRY-12] 제목" (미정이면 CRY-TBD)
 - 노트 간 참조는 위키링크 [[노트이름]]
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import yaml

ENV_VAR = "OBSIDIAN_AUTOJI_PATH"
TESTBED_DIR = "crypto_testbed"
SUBFOLDERS = ("01-decisions", "02-experiments", "03-daily-logs", "04-incidents")
INDEX_NAME = "00-index.md"
DEFAULT_ISSUE = "CRY-TBD"
KST = ZoneInfo("Asia/Seoul")
REPO_ROOT = Path(__file__).resolve().parents[1]
_UNSAFE_CHARS = re.compile(r'[\\/:*?"<>|\[\]#^\s]+')


class ObsidianVaultError(RuntimeError):
    """볼트 경로 없음 — 로그를 남길 수 없으므로 작업을 중단해야 함."""


def testbed_root(environ: dict | None = None) -> Path:
    env = os.environ if environ is None else environ
    raw = (env.get(ENV_VAR) or "").strip()
    if not raw:
        raise ObsidianVaultError(
            f"환경변수 {ENV_VAR} 가 없습니다. 작업 로그 유실을 막기 위해 중단합니다.")
    root = Path(raw).expanduser() / TESTBED_DIR
    if not root.is_dir():
        raise ObsidianVaultError(
            f"{root} 폴더가 없습니다. {ENV_VAR} 경로와 {TESTBED_DIR}/ 폴더를 확인하세요.")
    for name in SUBFOLDERS:
        (root / name).mkdir(exist_ok=True)
    return root


def now_kst() -> datetime:
    return datetime.now(KST)


def safe_name(text: str) -> str:
    return _UNSAFE_CHARS.sub("-", text.strip()).strip("-")


def titled(title: str, issue_id: str | None = None) -> str:
    return f"[{issue_id or DEFAULT_ISSUE}] {title}"


def git_commit(repo: Path = REPO_ROOT) -> str:
    try:
        head = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=repo, check=True,
                              capture_output=True, text=True).stdout.strip()
        dirty = subprocess.run(["git", "status", "--porcelain"], cwd=repo, check=True,
                               capture_output=True, text=True).stdout.strip()
        return f"{head}-dirty" if dirty else head
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def _unique(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while (candidate := path.with_name(f"{path.stem}_{i}{path.suffix}")).exists():
        i += 1
    return candidate


def _render(meta: dict, title: str, body: str) -> str:
    fm = yaml.safe_dump(meta, allow_unicode=True, sort_keys=False, default_flow_style=None,
                        width=1000).strip()
    return f"---\n{fm}\n---\n\n# {title}\n\n{body.strip()}\n"


def write_experiment(name: str, meta: dict, body: str, *, issue_id: str | None = None,
                     when: datetime | None = None, root: Path | None = None) -> Path:
    """실험 노트. meta 에 데이터구간_시작/종료, 파라미터_*, 결과_* 키가 반드시 있어야 한다."""
    missing = [k for k in ("데이터구간_시작", "데이터구간_종료") if k not in meta]
    if not any(k.startswith("파라미터_") for k in meta):
        missing.append("파라미터_*")
    if not any(k.startswith("결과_") for k in meta):
        missing.append("결과_*")
    if missing:
        raise ValueError(f"실험 노트 frontmatter 필수 항목 누락: {missing}")
    root = root or testbed_root()
    when = when or now_kst()
    full = {"날짜": when.strftime("%Y-%m-%d"), "실험명": name, "linear": issue_id or DEFAULT_ISSUE,
            "실행시각": when.isoformat(timespec="seconds"), "커밋": git_commit(),
            **meta}
    full.setdefault("태그", ["crypto-testbed", "experiment"])
    path = _unique(root / "02-experiments" / f"{when:%Y-%m-%d}_{safe_name(name)}.md")
    path.write_text(_render(full, titled(name, issue_id), body), encoding="utf-8")
    return path


def write_decision(title: str, *, adopted: str, rejected: list[tuple[str, str]], body: str = "",
                   meta: dict | None = None, issue_id: str | None = None,
                   when: datetime | None = None, root: Path | None = None) -> Path:
    if not rejected:
        raise ValueError("결정 노트에는 기각한 대안과 기각 사유를 남겨야 합니다.")
    root = root or testbed_root()
    when = when or now_kst()
    full = {"날짜": when.strftime("%Y-%m-%d"), "유형": "설계결정",
            "linear": issue_id or DEFAULT_ISSUE, "상태": "채택", **(meta or {})}
    full.setdefault("태그", ["crypto-testbed", "decision"])
    rows = "\n".join(f"| {alt} | {reason} |" for alt, reason in rejected)
    text = (f"## 결정\n\n{adopted.strip()}\n\n## 기각한 대안\n\n| 대안 | 기각 사유 |\n|---|---|\n"
            f"{rows}\n\n{body.strip()}")
    path = _unique(root / "01-decisions" / f"{when:%Y-%m-%d}_{safe_name(title)}.md")
    path.write_text(_render(full, titled(title, issue_id), text), encoding="utf-8")
    return path


def write_incident(title: str, *, body: str, meta: dict | None = None,
                   issue_id: str | None = None, when: datetime | None = None,
                   root: Path | None = None) -> Path:
    root = root or testbed_root()
    when = when or now_kst()
    full = {"날짜": when.strftime("%Y-%m-%d"), "발생시각": when.isoformat(timespec="seconds"),
            "유형": "사고기록", "linear": issue_id or DEFAULT_ISSUE, "상태": "조사 필요",
            **(meta or {})}
    full.setdefault("태그", ["crypto-testbed", "incident"])
    path = _unique(root / "04-incidents" / f"{when:%Y-%m-%d}_{safe_name(title)}.md")
    path.write_text(_render(full, titled(title, issue_id), body), encoding="utf-8")
    return path


def append_daily_log(text: str, *, heading: str = "", when: datetime | None = None,
                     root: Path | None = None) -> Path:
    root = root or testbed_root()
    when = when or now_kst()
    path = root / "03-daily-logs" / f"{when:%Y-%m-%d}.md"
    if not path.exists():
        meta = {"날짜": when.strftime("%Y-%m-%d"), "태그": ["crypto-testbed", "daily-log"]}
        path.write_text(_render(meta, f"{when:%Y-%m-%d} 작업 로그", ""), encoding="utf-8")
    with path.open("a", encoding="utf-8") as fh:
        fh.write(f"\n## {when:%H:%M} {heading}".rstrip() + f"\n\n{text.strip()}\n")
    return path


def register_experiment_in_index(note: Path, summary: str, root: Path | None = None) -> Path:
    """00-index.md 의 '## 최근 실험' 섹션 맨 위에 링크 추가."""
    root = root or testbed_root()
    index = root / INDEX_NAME
    line = f"- [[{note.stem}]] — {summary}"
    text = index.read_text(encoding="utf-8") if index.exists() else "# 크립토 테스트베드\n"
    heading = "## 최근 실험"
    if heading in text:
        head, tail = text.split(heading, 1)
        text = f"{head}{heading}\n\n{line}{tail if tail.startswith(chr(10) * 2) else chr(10) + tail}"
        text = text.replace(f"{line}\n\n\n", f"{line}\n")
    else:
        text = text.rstrip() + f"\n\n{heading}\n\n{line}\n"
    index.write_text(text, encoding="utf-8")
    return index
