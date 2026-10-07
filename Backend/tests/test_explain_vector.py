import pytest

from eval.explain_vector import _question


def test_question_returns_matching_dataset_question():
    dataset = {
        "questions": [
            {"id": "q01", "question": "first"},
            {"id": "q02", "question": "second"},
        ]
    }

    assert _question(dataset, "q02") == "second"


def test_question_rejects_unknown_id():
    dataset = {"questions": [{"id": "q01", "question": "first"}]}

    with pytest.raises(SystemExit, match="available ids: q01"):
        _question(dataset, "missing")
