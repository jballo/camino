import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from app.services.search import (
    _rrf_fuse,
    _load_chunks,
    _vector_search,
    _fts_search,
    hybrid_search,
    SearchResult,
    DEFAULT_K,
)


# ── _rrf_fuse (pure logic, no mocks needed) ─────────────────────────

def test_rrf_fuse_single_list():
    ranked = [(10, 1), (20, 2), (30, 3)]
    result = _rrf_fuse(ranked)
    ids = [cid for cid, _ in result]
    assert ids == [10, 20, 30]


def test_rrf_fuse_boosts_overlap():
    vector = [(10, 1), (20, 2)]
    fts = [(20, 1), (30, 2)]
    result = _rrf_fuse(vector, fts, k=60)
    ids = [cid for cid, _ in result]
    assert ids[0] == 20, "chunk appearing in both lists should rank first"


def test_rrf_fuse_scores_add_correctly():
    vector = [(10, 1)]
    fts = [(10, 1)]
    result = _rrf_fuse(vector, fts, k=60)
    expected = 2 * (1.0 / (60 + 1))
    assert abs(result[0][1] - expected) < 1e-9


def test_rrf_fuse_empty_lists():
    result = _rrf_fuse([], [])
    assert result == []


def test_rrf_fuse_no_overlap():
    vector = [(10, 1)]
    fts = [(20, 1)]
    result = _rrf_fuse(vector, fts, k=60)
    assert len(result) == 2
    scores = {cid: score for cid, score in result}
    assert abs(scores[10] - scores[20]) < 1e-9, "same rank ⇒ same score"


def test_rrf_fuse_three_lists():
    a = [(1, 1), (2, 2)]
    b = [(2, 1), (3, 2)]
    c = [(1, 1), (3, 2)]
    result = _rrf_fuse(a, b, c, k=60)
    ids = [cid for cid, _ in result]
    assert ids[0] in (1, 2), "chunks appearing in 2/3 lists should be at top"


# ── _vector_search ───────────────────────────────────────────────────

def test_vector_search_returns_ranked_tuples():
    mock_session = MagicMock()
    mock_row_1 = MagicMock(chunk_id=10, rank=1)
    mock_row_2 = MagicMock(chunk_id=20, rank=2)
    mock_session.execute.return_value.all.return_value = [mock_row_1, mock_row_2]

    result = _vector_search(mock_session, [0.1] * 1536, "org/repo", 1, 20)
    assert result == [(10, 1), (20, 2)]
    mock_session.execute.assert_called_once()


def test_vector_search_empty_result():
    mock_session = MagicMock()
    mock_session.execute.return_value.all.return_value = []

    result = _vector_search(mock_session, [0.1] * 1536, "org/repo", 1, 20)
    assert result == []


# ── _fts_search ──────────────────────────────────────────────────────

def test_fts_search_returns_ranked_tuples():
    mock_session = MagicMock()
    mock_row_1 = MagicMock(chunk_id=30, rank=1)
    mock_row_2 = MagicMock(chunk_id=40, rank=2)
    mock_session.execute.return_value.all.return_value = [mock_row_1, mock_row_2]

    result = _fts_search(mock_session, "authenticate", "org/repo", 1, 20)
    assert result == [(30, 1), (40, 2)]
    mock_session.execute.assert_called_once()


def test_fts_search_empty_result():
    mock_session = MagicMock()
    mock_session.execute.return_value.all.return_value = []

    result = _fts_search(mock_session, "nonexistent", "org/repo", 1, 20)
    assert result == []


# ── _load_chunks ─────────────────────────────────────────────────────

FAKE_ROW = {
    "id": 10,
    "repo_name": "org/repo",
    "file_path": "src/auth.py",
    "symbol_name": "login",
    "symbol_type": "function",
    "language": "py",
    "start_line": 1,
    "end_line": 10,
    "source_code": "def login(): ...",
    "signature": "def login():",
    "docstring": "Handles login.",
}


def test_load_chunks_returns_search_results():
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = [FAKE_ROW]

    fused = [(10, 0.033)]
    results = _load_chunks(mock_session, fused, limit=10)
    assert len(results) == 1
    assert isinstance(results[0], SearchResult)
    assert results[0].chunk_id == 10
    assert results[0].score == 0.033


def test_load_chunks_preserves_fused_order():
    row_a = {**FAKE_ROW, "id": 10, "symbol_name": "a"}
    row_b = {**FAKE_ROW, "id": 20, "symbol_name": "b"}
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = [row_b, row_a]

    fused = [(10, 0.05), (20, 0.03)]
    results = _load_chunks(mock_session, fused, limit=10)
    assert [r.chunk_id for r in results] == [10, 20]


def test_load_chunks_respects_limit():
    rows = [{**FAKE_ROW, "id": i} for i in range(5)]
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = rows

    fused = [(i, 1.0 / (60 + i)) for i in range(5)]
    results = _load_chunks(mock_session, fused, limit=2)
    assert len(results) == 2


def test_load_chunks_empty_fused():
    mock_session = MagicMock()
    results = _load_chunks(mock_session, [], limit=10)
    assert results == []
    mock_session.execute.assert_not_called()


def test_load_chunks_skips_missing_ids():
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = [FAKE_ROW]

    fused = [(10, 0.05), (999, 0.03)]
    results = _load_chunks(mock_session, fused, limit=10)
    assert len(results) == 1
    assert results[0].chunk_id == 10


# ── hybrid_search (integration of all pieces) ───────────────────────

@pytest.mark.asyncio
@patch("app.services.search._fts_search")
@patch("app.services.search._vector_search")
@patch("app.services.search.embed_batch", new_callable=AsyncMock)
async def test_hybrid_search_calls_both_retrievers(
    mock_embed, mock_vector, mock_fts
):
    mock_embed.return_value = [[0.1] * 1536]
    mock_vector.return_value = [(10, 1)]
    mock_fts.return_value = [(10, 1)]

    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = [FAKE_ROW]

    results = await hybrid_search(mock_session, "login", "org/repo", installation_id=1)

    mock_embed.assert_called_once_with(["login"])
    mock_vector.assert_called_once()
    mock_fts.assert_called_once()
    assert len(results) == 1


@pytest.mark.asyncio
@patch("app.services.search._fts_search")
@patch("app.services.search._vector_search")
@patch("app.services.search.embed_batch", new_callable=AsyncMock)
async def test_hybrid_search_empty_results(mock_embed, mock_vector, mock_fts):
    mock_embed.return_value = [[0.1] * 1536]
    mock_vector.return_value = []
    mock_fts.return_value = []

    mock_session = MagicMock()
    results = await hybrid_search(mock_session, "nothing", "org/repo", installation_id=1)
    assert results == []


@pytest.mark.asyncio
@patch("app.services.search._fts_search")
@patch("app.services.search._vector_search")
@patch("app.services.search.embed_batch", new_callable=AsyncMock)
async def test_hybrid_search_respects_limit(mock_embed, mock_vector, mock_fts):
    mock_embed.return_value = [[0.1] * 1536]
    mock_vector.return_value = [(i, i) for i in range(1, 11)]
    mock_fts.return_value = [(i, i) for i in range(1, 11)]

    rows = [{**FAKE_ROW, "id": i} for i in range(1, 11)]
    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = rows

    results = await hybrid_search(mock_session, "query", "org/repo", installation_id=1, limit=3)
    assert len(results) == 3


@pytest.mark.asyncio
@patch("app.services.search._fts_search")
@patch("app.services.search._vector_search")
@patch("app.services.search.embed_batch", new_callable=AsyncMock)
async def test_hybrid_search_vector_only_when_fts_empty(
    mock_embed, mock_vector, mock_fts
):
    mock_embed.return_value = [[0.1] * 1536]
    mock_vector.return_value = [(10, 1)]
    mock_fts.return_value = []

    mock_session = MagicMock()
    mock_session.execute.return_value.mappings.return_value.all.return_value = [FAKE_ROW]

    results = await hybrid_search(mock_session, "query", "org/repo", installation_id=1)
    assert len(results) == 1
    assert results[0].chunk_id == 10
