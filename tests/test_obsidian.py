from datetime import datetime

import pytest
import yaml

from src import obsidian
from src.obsidian import ObsidianVaultError

WHEN = datetime(2026, 9, 16, 9, 30, tzinfo=obsidian.KST)


@pytest.fixture
def vault(tmp_path, monkeypatch):
    (tmp_path / "crypto_testbed").mkdir()
    monkeypatch.setenv(obsidian.ENV_VAR, str(tmp_path))
    return obsidian.testbed_root()


def _frontmatter(path):
    text = path.read_text(encoding="utf-8")
    return yaml.safe_load(text.split("---")[1]), text


def test_missing_env_is_hard_fail(monkeypatch):
    monkeypatch.delenv(obsidian.ENV_VAR, raising=False)
    with pytest.raises(ObsidianVaultError):
        obsidian.testbed_root()


def test_missing_testbed_folder_is_hard_fail(tmp_path, monkeypatch):
    monkeypatch.setenv(obsidian.ENV_VAR, str(tmp_path))
    with pytest.raises(ObsidianVaultError):
        obsidian.testbed_root()


def test_subfolders_created(vault):
    for name in obsidian.SUBFOLDERS:
        assert (vault / name).is_dir()


def test_experiment_note_requires_metadata(vault):
    with pytest.raises(ValueError):
        obsidian.write_experiment("x", {"파라미터_a": 1}, "body", when=WHEN, root=vault)


def test_experiment_note_filename_frontmatter_and_collision(vault):
    meta = {"데이터구간_시작": "a", "데이터구간_종료": "b", "파라미터_심볼": "EWYUSDT", "결과_행": 3}
    p1 = obsidian.write_experiment("정합성 검사/EWY", meta, "본문 [[다른노트]]", issue_id="CRY-12",
                                   when=WHEN, root=vault)
    p2 = obsidian.write_experiment("정합성 검사/EWY", meta, "본문", when=WHEN, root=vault)
    assert p1.name == "2026-09-16_정합성-검사-EWY.md"
    assert p2.name == "2026-09-16_정합성-검사-EWY_2.md"
    fm, text = _frontmatter(p1)
    assert fm["linear"] == "CRY-12" and fm["결과_행"] == 3 and "커밋" in fm and "실행시각" in fm
    assert "# [CRY-12] 정합성 검사/EWY" in text and "[[다른노트]]" in text


def test_decision_requires_rejected_alternatives(vault):
    with pytest.raises(ValueError):
        obsidian.write_decision("스키마", adopted="A", rejected=[], root=vault)
    p = obsidian.write_decision("스키마", adopted="wide 컬럼", rejected=[("배열 컬럼", "SQL 불편")],
                                when=WHEN, root=vault)
    _, text = _frontmatter(p)
    assert "| 배열 컬럼 | SQL 불편 |" in text and "# [CRY-TBD] 스키마" in text


def test_daily_log_appends(vault):
    p = obsidian.append_daily_log("첫째", heading="A", when=WHEN, root=vault)
    obsidian.append_daily_log("둘째", heading="B", when=WHEN, root=vault)
    text = p.read_text(encoding="utf-8")
    assert p.name == "2026-09-16.md" and text.count("## 09:30") == 2
    assert text.index("첫째") < text.index("둘째")


def test_register_experiment_in_index(vault):
    (vault / "00-index.md").write_text("# 대시보드\n\n## 최근 실험\n\n- [[old]] — 이전\n\n## 설계 결정\n",
                                       encoding="utf-8")
    note = vault / "02-experiments" / "2026-09-16_new.md"
    obsidian.register_experiment_in_index(note, "요약", root=vault)
    text = (vault / "00-index.md").read_text(encoding="utf-8")
    assert text.index("[[2026-09-16_new]] — 요약") < text.index("[[old]]") < text.index("## 설계 결정")
