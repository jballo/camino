from pathlib import Path

import pytest

from eval.structural.citations import parse_citations, validate_citations
from eval.structural.validate import CheckKind


def _write_repo(root: Path) -> None:
    (root / "fastapi").mkdir(parents=True)
    (root / "fastapi" / "routing.py").write_text(
        "\n".join(f"line {i}" for i in range(1, 11)) + "\n"
    )


def test_parse_citations_backtick_path_and_symbol():
    text = "See `fastapi/routing.py:APIRoute` for the handler."
    refs = parse_citations(text)
    assert len(refs) == 1
    assert refs[0].file_path == "fastapi/routing.py"
    assert refs[0].symbol == "APIRoute"
    assert refs[0].start_line is None


def test_parse_citations_line_range():
    text = "Defined in fastapi/routing.py:3-5 during request handling."
    refs = parse_citations(text)
    assert len(refs) == 1
    assert refs[0].start_line == 3
    assert refs[0].end_line == 5


def test_parse_citations_single_line():
    refs = parse_citations("Jump to `fastapi/routing.py:7`.")
    assert len(refs) == 1
    assert refs[0].start_line == 7
    assert refs[0].end_line == 7


def test_parse_citations_deduplicates():
    text = "`fastapi/routing.py:APIRoute` and fastapi/routing.py:APIRoute"
    refs = parse_citations(text)
    assert len(refs) == 1


def test_parse_citations_keeps_plain_path_alongside_qualified_ref():
    text = "`fastapi/routing.py:APIRoute` is defined in fastapi/routing.py."
    refs = parse_citations(text)
    keys = {ref.key for ref in refs}
    assert ("fastapi/routing.py", None, None, "APIRoute") in keys
    assert ("fastapi/routing.py", None, None, None) in keys


def test_parse_citations_ignores_paths_inside_urls():
    text = "See https://fastapi.tiangolo.com/tutorial/first-steps.md for the guide."
    assert parse_citations(text) == []


def test_parse_citations_keeps_real_citation_next_to_a_url():
    text = "Docs at https://fastapi.tiangolo.com/x.md but code is `fastapi/routing.py:7`."
    refs = parse_citations(text)
    assert len(refs) == 1
    assert refs[0].file_path == "fastapi/routing.py"
    assert refs[0].start_line == 7


def test_validate_citations_passes_for_existing_path(tmp_path: Path):
    _write_repo(tmp_path)
    refs = parse_citations("`fastapi/routing.py:APIRoute`")
    result = validate_citations(refs, tmp_path)
    assert result.passed


def test_validate_citations_rejects_missing_path(tmp_path: Path):
    _write_repo(tmp_path)
    refs = parse_citations("fastapi/missing.py:Foo")
    result = validate_citations(refs, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.PATH_EXISTS}


def test_validate_citations_rejects_out_of_bounds_lines(tmp_path: Path):
    _write_repo(tmp_path)
    refs = parse_citations("fastapi/routing.py:99-100")
    result = validate_citations(refs, tmp_path)
    assert not result.passed
    assert result.failed_checks == {CheckKind.LINES_IN_BOUNDS}


@pytest.mark.asyncio
async def test_run_agent_smoke_eval_orchestration(tmp_path: Path, monkeypatch):
    from eval import run_agent_smoke_eval as smoke

    _write_repo(tmp_path)

    async def fake_answer_question(session, **kwargs):
        from app.agent.runner import AgentAnswer

        return AgentAnswer(
            answer="See `fastapi/routing.py:3-5` for details.",
            sources=[],
        )

    monkeypatch.setattr(smoke, "answer_question", fake_answer_question)
    monkeypatch.setattr(smoke, "_chunk_count", lambda *args, **kwargs: 42)

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    monkeypatch.setattr(smoke, "Session", lambda engine: FakeSession())
    monkeypatch.setattr(smoke, "create_engine", lambda url: object())

    runs = await smoke._run_all(
        repo_root=tmp_path,
        repo_name="tiangolo/fastapi",
        installation_id=999999999,
        questions=[{"id": "q02", "question": "Where is OpenAPI built?"}],
        model=None,
        search_limit=8,
    )

    assert len(runs) == 1
    assert runs[0].citation_count == 1
    assert runs[0].citations_valid


@pytest.mark.asyncio
async def test_run_question_isolates_answer_question_failure(tmp_path: Path, monkeypatch):
    from eval import run_agent_smoke_eval as smoke

    _write_repo(tmp_path)

    async def boom(session, **kwargs):
        raise RuntimeError("rate limit exceeded")

    monkeypatch.setattr(smoke, "answer_question", boom)

    class FakeSession:
        def __init__(self):
            self.rolled_back = False

        def rollback(self):
            self.rolled_back = True

    session = FakeSession()
    run = await smoke._run_question(
        session,
        question={"id": "q01", "question": "Where is OpenAPI built?"},
        repo_name="tiangolo/fastapi",
        installation_id=999999999,
        repo_root=tmp_path,
        model=None,
        search_limit=8,
    )

    assert run.id == "q01"
    assert not run.citations_valid
    assert run.failed_checks == ["harness_error"]
    assert "rate limit exceeded" in run.answer_preview
    assert run.issues[0]["kind"] == "harness_error"
    # a poisoned shared transaction must be rolled back before the next question
    assert session.rolled_back


def _question_run(smoke, *, citation_count: int, citations_valid: bool):
    return smoke.QuestionRun(
        id="q01",
        question="q",
        answer_preview="",
        citations=[],
        citation_count=citation_count,
        citations_valid=citations_valid,
        failed_checks=[],
        issues=[],
        source_count=0,
        elapsed_s=0.0,
    )


def _strict_fails(aggregate) -> bool:
    """Mirror of the --strict gate in main()."""
    return (
        aggregate["questions_all_citations_valid"] != aggregate["questions"]
        or aggregate["questions_with_citations"] != aggregate["questions"]
    )


def test_strict_gate_fails_when_a_question_has_no_citations():
    from eval import run_agent_smoke_eval as smoke

    # Zero-citation answers vacuously pass validation but must still fail --strict,
    # otherwise a regression that strips all citations goes undetected.
    runs = [_question_run(smoke, citation_count=0, citations_valid=True)]
    aggregate = smoke._aggregate(runs)

    assert aggregate["questions_all_citations_valid"] == aggregate["questions"]
    assert aggregate["questions_with_citations"] == 0
    assert _strict_fails(aggregate)


def test_strict_gate_passes_when_all_questions_cite_validly():
    from eval import run_agent_smoke_eval as smoke

    runs = [_question_run(smoke, citation_count=2, citations_valid=True)]
    aggregate = smoke._aggregate(runs)

    assert not _strict_fails(aggregate)
