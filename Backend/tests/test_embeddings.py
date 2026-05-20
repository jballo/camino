from unittest.mock import patch, MagicMock

from app.services.parser import CodeChunk
from app.services.embeddings import build_embedding_text, embed_batch, embed_all


FUNC_CHUNK = CodeChunk(
    file_path="src/auth/handler.py",
    symbol_name="authenticate_user",
    symbol_type="function",
    language="py",
    start_line=10,
    end_line=25,
    source_code=(
        "def authenticate_user(username: str, password: str) -> bool:\n"
        '    """Verify credentials against the database."""\n'
        "    user = db.query(User).filter_by(username=username).first()\n"
        "    if user is None:\n"
        "        return False\n"
        "    return check_password(password, user.hashed_password)\n"
    ),
    signature="def authenticate_user(username: str, password: str) -> bool:",
    docstring="Verify credentials against the database.",
    parent_class=None,
)

NO_DOC_CHUNK = CodeChunk(
    file_path="utils/math.py",
    symbol_name="add",
    symbol_type="function",
    language="py",
    start_line=1,
    end_line=2,
    source_code="def add(a: int, b: int) -> int:\n    return a + b\n",
    signature="def add(a: int, b: int) -> int:",
    docstring=None,
    parent_class=None,
)

STUB_CHUNK = CodeChunk(
    file_path="models/base.py",
    symbol_name="BaseModel",
    symbol_type="class",
    language="py",
    start_line=1,
    end_line=2,
    source_code="class BaseModel:\n    pass\n",
    signature="class BaseModel:",
    docstring=None,
    parent_class=None,
)


def test_embedding_text_includes_signature():
    text = build_embedding_text(FUNC_CHUNK)
    assert text.startswith("def authenticate_user")


def test_embedding_text_includes_docstring():
    text = build_embedding_text(FUNC_CHUNK)
    assert "Verify credentials against the database." in text


def test_embedding_text_includes_body_preview():
    text = build_embedding_text(FUNC_CHUNK)
    assert "db.query(User)" in text


def test_embedding_text_no_duplicate_signature():
    text = build_embedding_text(FUNC_CHUNK)
    sig = "def authenticate_user(username: str, password: str) -> bool:"
    assert text.count(sig) == 1


def test_embedding_text_no_duplicate_docstring():
    text = build_embedding_text(FUNC_CHUNK)
    assert text.count("Verify credentials against the database.") == 1


def test_embedding_text_without_docstring():
    text = build_embedding_text(NO_DOC_CHUNK)
    assert text.startswith("def add")
    assert "return a + b" in text


def test_embedding_text_stub_only_has_signature():
    text = build_embedding_text(STUB_CHUNK)
    assert "class BaseModel:" in text
    assert "pass" in text


def test_body_preview_respects_max_lines():
    long_source = "def big():\n" + "".join(f"    line_{i}\n" for i in range(50))
    chunk = CodeChunk(
        file_path="big.py",
        symbol_name="big",
        symbol_type="function",
        language="py",
        start_line=1,
        end_line=51,
        source_code=long_source,
        signature="def big():",
        docstring=None,
        parent_class=None,
    )
    text = build_embedding_text(chunk, max_body_lines=5)
    body_section = text.split("\n", 1)[1]
    assert body_section.count("\n") <= 4


def _make_mock_response(texts: list[str], dim: int = 1536):
    mock_response = MagicMock()
    mock_response.data = [
        MagicMock(embedding=[0.1] * dim) for _ in texts
    ]
    return mock_response


@patch("app.services.embeddings.client")
def test_embed_batch_returns_correct_count(mock_client):
    texts = ["hello", "world"]
    mock_client.embeddings.create.return_value = _make_mock_response(texts)
    result = embed_batch(texts)
    assert len(result) == 2
    assert len(result[0]) == 1536


@patch("app.services.embeddings.client")
def test_embed_batch_passes_texts_to_api(mock_client):
    texts = ["hello", "world"]
    mock_client.embeddings.create.return_value = _make_mock_response(texts)
    embed_batch(texts)
    mock_client.embeddings.create.assert_called_once()
    call_kwargs = mock_client.embeddings.create.call_args
    assert call_kwargs.kwargs["input"] == texts


@patch("app.services.embeddings.client")
def test_embed_all_batches_correctly(mock_client):
    texts = [f"text_{i}" for i in range(300)]
    mock_client.embeddings.create.side_effect = lambda **kwargs: _make_mock_response(kwargs["input"])
    result = embed_all(texts)
    assert len(result) == 300
    assert mock_client.embeddings.create.call_count == 2


@patch("app.services.embeddings.client")
def test_embed_all_empty_list(mock_client):
    result = embed_all([])
    assert result == []
    mock_client.embeddings.create.assert_not_called()
