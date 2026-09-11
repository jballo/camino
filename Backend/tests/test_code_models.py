from sqlalchemy import UniqueConstraint

from app.models.code import CodeChunkModel, RepoIndexState


def _unique_constraint(model, name: str) -> UniqueConstraint:
    return next(
        constraint
        for constraint in model.__table__.constraints
        if isinstance(constraint, UniqueConstraint) and constraint.name == name
    )


def test_chunk_identity_is_unique_within_a_generation():
    constraint = _unique_constraint(
        CodeChunkModel,
        "uq_chunk_identity_gen",
    )

    assert [column.name for column in constraint.columns] == [
        "installation_id",
        "repo_name",
        "generation",
        "file_path",
        "symbol_name",
        "start_line",
    ]


def test_repo_index_state_has_one_pointer_per_repository():
    constraint = _unique_constraint(
        RepoIndexState,
        "uq_repo_index_state",
    )

    assert [column.name for column in constraint.columns] == [
        "installation_id",
        "repo_name",
    ]
