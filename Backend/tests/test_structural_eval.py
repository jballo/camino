import json
from pathlib import Path

import pytest

from app.models.tour import TourArtifact, TourStep
from eval.structural.validate import (
    CheckKind,
    normalize_text,
    parse_tour_payload,
    validate_tour,
    validate_tour_artifact,
)


def _sample_tour(**overrides) -> dict:
    base = {
        "title": "Sample tour",
        "topic": "demo",
        "repo_name": "org/repo",
        "steps": [
            {
                "title": "Entrypoint",
                "explanation": "Main module.",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 2,
                "snippet": "def main():\n    return 42",
                "why": "Starts the program.",
            }
        ],
    }
    base.update(overrides)
    return base


def _write_repo(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text("def main():\n    return 42\n")


def test_normalize_text_strips_trailing_whitespace():
    assert normalize_text("a  \n  b\n") == "a\n  b"


def test_parse_tour_payload_rejects_invalid_json():
    artifact, issues = parse_tour_payload("{not json")
    assert artifact is None
    assert len(issues) == 1
    assert issues[0].kind == CheckKind.SCHEMA


def test_parse_tour_payload_rejects_empty_steps():
    artifact, issues = parse_tour_payload(_sample_tour(steps=[]))
    assert artifact is None
    assert issues
    assert all(issue.kind == CheckKind.SCHEMA for issue in issues)


def test_validate_tour_artifact_passes_for_matching_repo(tmp_path: Path):
    _write_repo(tmp_path)
    artifact = TourArtifact.model_validate(_sample_tour())
    result = validate_tour_artifact(artifact, tmp_path)
    assert result.passed
    assert result.issues == []


def test_validate_tour_rejects_missing_path(tmp_path: Path):
    _write_repo(tmp_path)
    payload = _sample_tour(
        steps=[
            {
                "title": "Missing",
                "explanation": "No file.",
                "file_path": "src/missing.py",
                "start_line": 1,
                "end_line": 1,
                "snippet": "pass",
            }
        ]
    )
    result = validate_tour(payload, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.PATH_EXISTS}


def test_validate_tour_rejects_out_of_bounds_lines(tmp_path: Path):
    _write_repo(tmp_path)
    payload = _sample_tour(
        steps=[
            {
                "title": "Too far",
                "explanation": "Past EOF.",
                "file_path": "src/app.py",
                "start_line": 99,
                "end_line": 99,
                "snippet": "def main():",
            }
        ]
    )
    result = validate_tour(payload, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.LINES_IN_BOUNDS}


def test_validate_tour_rejects_wrong_snippet(tmp_path: Path):
    _write_repo(tmp_path)
    payload = _sample_tour(
        steps=[
            {
                "title": "Wrong quote",
                "explanation": "Bad snippet.",
                "file_path": "src/app.py",
                "start_line": 1,
                "end_line": 2,
                "snippet": "def other():",
            }
        ]
    )
    result = validate_tour(payload, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.SNIPPET_MATCHES}


def test_validate_tour_rejects_path_traversal(tmp_path: Path):
    _write_repo(tmp_path)
    payload = _sample_tour(
        steps=[
            {
                "title": "Escape",
                "explanation": "Outside repo.",
                "file_path": "../outside.py",
                "start_line": 1,
                "end_line": 1,
                "snippet": "x",
            }
        ]
    )
    result = validate_tour(payload, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.PATH_EXISTS}


def test_tour_step_rejects_end_before_start():
    with pytest.raises(ValueError, match="end_line"):
        TourStep(
            title="Bad range",
            explanation="Invalid.",
            file_path="src/app.py",
            start_line=10,
            end_line=5,
            snippet="x",
        )


def test_valid_minimal_fixture_against_fastapi_repo():
    """Requires eval/.data/fastapi — skipped when the fixture is not present."""
    repo_root = Path(__file__).resolve().parents[1] / "eval" / ".data" / "fastapi"
    if not repo_root.exists():
        pytest.skip("FastAPI fixture not checked out")

    fixture_path = (
        Path(__file__).resolve().parents[1]
        / "eval"
        / "structural"
        / "fixtures"
        / "valid_minimal.json"
    )
    result = validate_tour(fixture_path.read_text(), repo_root)
    assert result.passed, result.issues
