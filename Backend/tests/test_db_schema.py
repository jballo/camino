import logging
from unittest.mock import MagicMock

import pytest

from app.config import Settings, settings
from app.db_schema import verify_embedding_schema


def _scalar_result(value: str | None) -> MagicMock:
    result = MagicMock()
    result.scalar_one_or_none.return_value = value
    return result


def test_vector_defaults_use_halfvec_without_ann_index():
    assert Settings.model_fields["vector_type"].default == "halfvec"
    assert Settings.model_fields["vector_index"].default == "none"


def test_verify_embedding_schema_accepts_matching_type(monkeypatch):
    monkeypatch.setattr(settings, "vector_type", "halfvec")
    monkeypatch.setattr(settings, "vector_index", "none")
    connection = MagicMock()
    connection.execute.side_effect = [
        _scalar_result("halfvec(1536)"),
        _scalar_result(None),
    ]

    verify_embedding_schema(connection)

    assert connection.execute.call_count == 2


def test_verify_embedding_schema_rejects_mismatched_type(monkeypatch):
    monkeypatch.setattr(settings, "vector_type", "halfvec")
    connection = MagicMock()
    connection.execute.return_value = _scalar_result("vector(1536)")

    with pytest.raises(
        RuntimeError,
        match=r"database column type is 'vector\(1536\)'.*expects 'halfvec\(1536\)'",
    ):
        verify_embedding_schema(connection)


def test_verify_embedding_schema_warns_about_unused_hnsw(
    monkeypatch,
    caplog,
):
    monkeypatch.setattr(settings, "vector_type", "halfvec")
    monkeypatch.setattr(settings, "vector_index", "none")
    connection = MagicMock()
    connection.execute.side_effect = [
        _scalar_result("halfvec(1536)"),
        _scalar_result("ix_embeddings_hnsw"),
    ]

    with caplog.at_level(logging.WARNING, logger="app.db_schema"):
        verify_embedding_schema(connection)

    assert "ix_embeddings_hnsw exists" in caplog.text
    assert "unused storage" in caplog.text
