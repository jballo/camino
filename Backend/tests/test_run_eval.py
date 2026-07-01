import pytest

from eval.run_eval import RetrievalConfig, _print_report, main


def test_retrieval_config_defaults_match_cli_experiment_defaults():
    cfg = RetrievalConfig()

    assert cfg.top_n == 60
    assert cfg.path_penalty == 0.3


def test_print_report_tolerates_legacy_config_without_rerank_keys(capsys):
    report = {
        "config": {
            "mode": "hybrid",
            "k": 5,
            "limit": 10,
            "top_n": 60,
            "rrf_k": 60,
            "vector_weight": 1.0,
            "fts_weight": 1.0,
            "path_penalty": 0.3,
            "filter_demo_paths": True,
        },
        "aggregate": {
            "questions": 1,
            "hit_rate@5": 1.0,
            "recall@5": 1.0,
            "precision@5": 0.2,
            "mrr": 1.0,
        },
        "per_question": [
            {
                "id": "q01",
                "question": "How does FastAPI handle dependency injection?",
                "hit": 1.0,
                "recall": 1.0,
                "precision": 0.2,
                "rr": 1.0,
                "diagnosis": [],
            }
        ],
    }

    _print_report(report, label="legacy")

    out = capsys.readouterr().out
    assert "rerank=False rerank_top_n=30 rerank_rrf_w=0.9" in out


def test_main_rejects_out_of_range_rerank_rrf_weight(monkeypatch):
    monkeypatch.setattr("sys.argv", ["run_eval", "--rerank-rrf-weight", "1.5"])

    with pytest.raises(SystemExit, match="--rerank-rrf-weight 1.5 is out of range"):
        main()
