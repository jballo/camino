import pytest
from unittest.mock import MagicMock, patch

from app.services.rerank import (
    DEFAULT_RERANK_RRF_WEIGHT,
    _build_rerank_text,
    rerank_results,
    validate_rrf_weight,
)
from app.services.search import SearchResult


def _result(chunk_id: int, score: float) -> SearchResult:
    return SearchResult(
        chunk_id=chunk_id,
        repo_name="org/repo",
        file_path=f"src/{chunk_id}.py",
        symbol_name=f"fn_{chunk_id}",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=2,
        source_code="def fn(): pass",
        signature="def fn():",
        docstring=None,
        score=score,
    )


@pytest.mark.parametrize("weight", [-0.1, 1.5])
def test_validate_rrf_weight_rejects_out_of_range(weight):
    with pytest.raises(ValueError, match="rrf_weight must be between 0.0 and 1.0"):
        validate_rrf_weight(weight)


@patch("app.services.rerank._get_cross_encoder")
def test_rerank_results_falls_back_on_invalid_rrf_weight(mock_get_encoder):
    results = [_result(1, 0.9), _result(2, 0.5)]

    reranked = rerank_results("query", results, top_n=2, rrf_weight=1.5)

    assert reranked == results
    mock_get_encoder.assert_not_called()


def test_rerank_results_uses_shared_default_rrf_weight():
    assert DEFAULT_RERANK_RRF_WEIGHT == 0.9


@patch("app.services.rerank._get_cross_encoder")
def test_rerank_results_returns_rrf_order_when_top_n_is_zero(mock_get_encoder):
    results = [_result(1, 0.9), _result(2, 0.5)]

    reranked = rerank_results("query", results, top_n=0)

    assert [r.chunk_id for r in reranked] == [1, 2]
    mock_get_encoder.assert_not_called()


@patch("app.services.rerank._get_cross_encoder")
def test_rerank_results_falls_back_on_predict_failure(mock_get_encoder):
    results = [_result(1, 0.9), _result(2, 0.5)]
    original_order = [r.chunk_id for r in results]

    model = MagicMock()
    model.predict.side_effect = RuntimeError("model unavailable")
    mock_get_encoder.return_value = model

    reranked = rerank_results("query", results, top_n=2)

    assert [r.chunk_id for r in reranked] == original_order


@patch("app.services.rerank._get_cross_encoder")
def test_rerank_results_preserves_original_rrf_scores(mock_get_encoder):
    results = [_result(1, 0.9), _result(2, 0.5), _result(3, 0.1)]
    original_scores = [r.score for r in results]

    model = MagicMock()
    model.predict.return_value = [0.2, 0.8, 0.5]
    mock_get_encoder.return_value = model

    reranked = rerank_results("query", results, top_n=3, rrf_weight=0.5)

    assert [r.score for r in results] == original_scores
    assert [r.chunk_id for r in reranked] == [2, 1, 3]
    reranked_by_id = {r.chunk_id: r.score for r in reranked}
    for r in results:
        assert reranked_by_id[r.chunk_id] != r.score


def test_build_rerank_text_skips_body_preview_when_source_code_is_none():
    result = SearchResult(
        chunk_id=1,
        repo_name="org/repo",
        file_path="src/binary.bin",
        symbol_name="read_file",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=1,
        source_code=None,
        signature="def read_file():",
        docstring="Load binary payload.",
        score=0.5,
    )

    text = _build_rerank_text(result)

    assert "function read_file" in text
    assert "src/binary.bin" in text
    assert "def read_file():" in text
    assert "Load binary payload." in text


@patch("app.services.rerank._get_cross_encoder")
def test_rerank_results_handles_null_source_code(mock_get_encoder):
    results = [
        SearchResult(
            chunk_id=1,
            repo_name="org/repo",
            file_path="src/binary.bin",
            symbol_name="read_file",
            symbol_type="function",
            language="python",
            start_line=1,
            end_line=1,
            source_code=None,
            signature="def read_file():",
            docstring=None,
            score=0.9,
        ),
        _result(2, 0.5),
    ]

    model = MagicMock()
    model.predict.return_value = [0.2, 0.8]
    mock_get_encoder.return_value = model

    reranked = rerank_results("query", results, top_n=2, rrf_weight=0.5)

    assert len(reranked) == 2
    assert model.predict.call_count == 1


def test_build_rerank_text_body_preview_skips_multiline_signature_and_docstring():
    result = SearchResult(
        chunk_id=1,
        repo_name="org/repo",
        file_path="src/auth.py",
        symbol_name="authenticate_user",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=10,
        source_code=(
            "def authenticate_user(\n"
            "    username: str,\n"
            "    password: str,\n"
            ") -> bool:\n"
            '    """\n'
            "    Verify credentials against the database.\n"
            '    """\n'
            "    user = db.query(User).filter_by(username=username).first()\n"
            "    return user is not None\n"
        ),
        signature="def authenticate_user(username: str, password: str) -> bool:",
        docstring="Verify credentials against the database.",
        score=0.5,
    )

    text = _build_rerank_text(result)

    assert "user = db.query(User)" in text
    assert "Verify credentials against the database." in text
    # docstring is included once (via parts); the body preview must not repeat it
    assert text.count("Verify credentials against the database.") == 1
    assert '"""' not in text
    assert "username: str" not in text.split("Verify credentials")[-1]


def test_build_rerank_text_signature_skip_ignores_nested_colon_lines():
    result = SearchResult(
        chunk_id=1,
        repo_name="org/repo",
        file_path="src/routes.py",
        symbol_name="configure",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=9,
        source_code=(
            "def configure(\n"
            "    routes={\n"
            '        "GET":\n'
            '            "handler",\n'
            "    },\n"
            ") -> None:\n"
            "    return routes\n"
        ),
        signature='def configure(routes={"GET": "handler"}) -> None:',
        docstring=None,
        score=0.5,
    )

    text = _build_rerank_text(result)

    assert "return routes" in text
    assert '\n            "handler",' not in text
    assert "\n) -> None:" not in text


def test_build_rerank_text_signature_skip_ignores_brackets_in_strings_and_comments():
    result = SearchResult(
        chunk_id=1,
        repo_name="org/repo",
        file_path="src/routes.py",
        symbol_name="make_route",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=7,
        source_code=(
            "def make_route(\n"
            '    path: str = "open paren (",\n'
            "    handler=None,  # (internal note\n"
            ") -> None:\n"
            "    return handler\n"
        ),
        signature='def make_route(path: str = "open paren (", handler=None) -> None:',
        docstring=None,
        score=0.5,
    )

    text = _build_rerank_text(result)

    assert "return handler" in text
    assert "\n    handler=None" not in text
    assert "\n) -> None:" not in text


def test_build_rerank_text_docstring_skip_ignores_escaped_delimiter():
    result = SearchResult(
        chunk_id=1,
        repo_name="org/repo",
        file_path="src/docs.py",
        symbol_name="explain",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=6,
        source_code=(
            "def explain():\n"
            '    """\n'
            '    Mention \\""" inside docs.\n'
            '    """\n'
            "    return 1\n"
        ),
        signature="def explain():",
        docstring='Mention \\""" inside docs.',
        score=0.5,
    )

    text = _build_rerank_text(result)

    assert "return 1" in text
    assert text.count('Mention \\""" inside docs.') == 1
    assert '    """' not in text
