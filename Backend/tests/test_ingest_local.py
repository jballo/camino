from unittest.mock import AsyncMock, MagicMock, patch

from app.services.parser import CodeChunk
from eval.ingest_local import ingest


async def test_ingest_writes_ref_keyed_chunks_and_index_state(tmp_path):
    source = tmp_path / "example.py"
    source.write_text("def greet():\n    return 'hello'\n")
    parsed_chunk = CodeChunk(
        file_path="example.py",
        symbol_name="greet",
        symbol_type="function",
        language="python",
        start_line=1,
        end_line=2,
        source_code="def greet():\n    return 'hello'",
        signature="def greet():",
        docstring=None,
        parent_class=None,
    )
    session = MagicMock()
    session_context = MagicMock()
    session_context.__enter__.return_value = session

    with (
        patch("eval.ingest_local.create_engine"),
        patch("eval.ingest_local.Session", return_value=session_context),
        patch("eval.ingest_local._git_commit_sha", return_value="abc123"),
        patch("eval.ingest_local.parse_file", return_value=[parsed_chunk]),
        patch(
            "eval.ingest_local.embed_all",
            new_callable=AsyncMock,
            return_value=[[0.25]],
        ),
    ):
        result = await ingest(
            tmp_path,
            "Org/Repo",
            ref="release/1.0",
            visibility="private",
        )

    assert result == {"chunks": 1, "embeddings": 1}

    delete_statement = session.exec.call_args_list[0].args[0]
    delete_sql = " ".join(str(delete_statement).split())
    assert "code_chunks.repo_name" in delete_sql
    assert "code_chunks.ref" in delete_sql
    assert "installation_id" not in delete_sql

    chunks = session.add_all.call_args_list[0].args[0]
    assert len(chunks) == 1
    assert chunks[0].repo_name == "org/repo"
    assert chunks[0].ref == "release/1.0"
    assert chunks[0].generation

    search_vector_statement = session.exec.call_args_list[1].args[0]
    search_vector_params = search_vector_statement.compile().params
    assert search_vector_params["repo_name"] == "org/repo"
    assert search_vector_params["ref"] == "release/1.0"
    assert search_vector_params["generation"] == chunks[0].generation

    publish_statement = session.exec.call_args_list[2].args[0]
    publish_sql = " ".join(str(publish_statement).split())
    publish_params = publish_statement.compile().params
    assert "ON CONFLICT (repo_name, ref)" in publish_sql
    assert "installation_id" not in publish_sql
    assert publish_params == {
        "repo_name": "org/repo",
        "ref": "release/1.0",
        "visibility": "private",
        "generation": chunks[0].generation,
        "commit_sha": "abc123",
    }
    session.commit.assert_called_once_with()
