from pathlib import Path

import pytest

from eval.analyze_dims import (
    DimensionRun,
    QuestionRanks,
    load_runs,
    metrics,
    paired_bootstrap_mrr_delta,
    pool_runs,
)


def _run(
    label: str,
    dimensions: int,
    ranks: tuple[int | None, ...],
    repo_name: str = "owner/repo",
    ref: str = "v1",
    mode: str = "hybrid",
) -> DimensionRun:
    return DimensionRun(
        path=Path(f"{label}.json"),
        label=label,
        mode=mode,
        dimensions=dimensions,
        repo_name=repo_name,
        ref=ref,
        questions=tuple(
            QuestionRanks(
                question_id=f"q{index}",
                question=f"question {index}",
                final_rank=rank,
                vector_rank=rank,
                relevant_ranks=(rank,),
            )
            for index, rank in enumerate(ranks, 1)
        ),
    )


def test_metrics_apply_requested_cutoff():
    run = _run("candidate", 512, (1, 6, None))

    assert metrics(run, 5) == {
        "hit_rate": pytest.approx(1 / 3),
        "recall": pytest.approx(1 / 3),
        "mrr": pytest.approx(1 / 3),
    }
    assert metrics(run, 10) == {
        "hit_rate": pytest.approx(2 / 3),
        "recall": pytest.approx(2 / 3),
        "mrr": pytest.approx((1 + 1 / 6) / 3),
    }


def test_paired_bootstrap_reports_observed_mrr_delta():
    baseline = _run("baseline", 1536, (1, 2, None))
    candidate = _run("candidate", 512, (1, 1, None))

    delta, low, high = paired_bootstrap_mrr_delta(
        candidate, baseline, cutoff=5, iterations=1_000, seed=8
    )

    assert delta == pytest.approx(1 / 6)
    assert low <= delta <= high


def test_paired_bootstrap_rejects_unpaired_question_sets():
    baseline = _run("baseline", 1536, (1, 2))
    candidate = _run("candidate", 512, (1,))

    with pytest.raises(ValueError, match="cannot pair"):
        paired_bootstrap_mrr_delta(candidate, baseline, cutoff=5)


def test_pool_runs_namespaces_questions_and_pools_per_question():
    runs = [
        _run("sweep_a_1536", 1536, (1, 2), repo_name="org/a"),
        _run("sweep_a_512", 512, (1, None), repo_name="org/a"),
        _run("b_1536", 1536, (1, 1, 1), repo_name="org/b"),
        _run("b_512", 512, (1, 1, 1), repo_name="org/b"),
    ]

    pooled = pool_runs(runs)

    assert set(pooled) == {"hybrid"}
    by_dim = {run.dimensions: run for run in pooled["hybrid"]}
    assert set(by_dim) == {1536, 512}
    assert [q.question_id for q in by_dim[1536].questions] == [
        "org/a@v1:q1",
        "org/a@v1:q2",
        "org/b@v1:q1",
        "org/b@v1:q2",
        "org/b@v1:q3",
    ]
    # Per-question pooling weights the 3-question corpus more than the
    # 2-question one; an unweighted average of per-repo MRRs would give 0.875.
    assert metrics(by_dim[1536], 5)["mrr"] == pytest.approx((1 + 0.5 + 3) / 5)
    delta, low, high = paired_bootstrap_mrr_delta(
        by_dim[512], by_dim[1536], cutoff=5
    )
    assert delta == pytest.approx((0 - 0.5 + 0) / 5)
    assert low <= delta <= high


def test_pool_runs_skips_dims_missing_from_a_corpus_and_prefers_sweep():
    runs = [
        _run("sweep_a_1536", 1536, (1,), repo_name="org/a"),
        _run("jitter_a_1536", 1536, (5,), repo_name="org/a"),
        _run("a_768", 768, (1,), repo_name="org/a"),
        _run("b_1536", 1536, (1,), repo_name="org/b"),
    ]

    pooled = pool_runs(runs)["hybrid"]

    assert [run.dimensions for run in pooled] == [1536]
    assert "sweep_a_1536" in pooled[0].ref
    assert "jitter_a_1536" not in pooled[0].ref
    ranks = {q.question_id: q.final_rank for q in pooled[0].questions}
    assert ranks["org/a@v1:q1"] == 1


def test_pool_runs_requires_two_corpora_and_a_full_dim_baseline():
    single_corpus = [
        _run("a_1536", 1536, (1,), repo_name="org/a"),
        _run("a_512", 512, (1,), repo_name="org/a"),
    ]
    assert pool_runs(single_corpus) == {}

    no_shared_baseline = [
        _run("a_512", 512, (1,), repo_name="org/a"),
        _run("b_512", 512, (1,), repo_name="org/b"),
        _run("b_1536", 1536, (1,), repo_name="org/b"),
    ]
    assert pool_runs(no_shared_baseline) == {}


def test_pool_runs_keeps_modes_separate():
    runs = [
        _run("a_h_1536", 1536, (1,), repo_name="org/a", mode="hybrid"),
        _run("b_h_1536", 1536, (2,), repo_name="org/b", mode="hybrid"),
        _run("a_v_1536", 1536, (3,), repo_name="org/a", mode="vector"),
        _run("b_v_1536", 1536, (4,), repo_name="org/b", mode="vector"),
    ]

    pooled = pool_runs(runs)

    assert set(pooled) == {"hybrid", "vector"}
    hybrid_ranks = sorted(
        q.final_rank for q in pooled["hybrid"][0].questions
    )
    vector_ranks = sorted(
        q.final_rank for q in pooled["vector"][0].questions
    )
    assert hybrid_ranks == [1, 2]
    assert vector_ranks == [3, 4]


def test_load_runs_rejects_all_unindexed_report(tmp_path):
    path = tmp_path / "invalid.json"
    path.write_text(
        """{
          "label": "invalid",
          "config": {"mode": "hybrid", "limit": 10, "vector_dims": 1536},
          "per_question": [{
            "id": "q01",
            "question": "question",
            "first_rank": null,
            "diagnosis": [{
              "indexed": false,
              "vector_rank": null,
              "final_rank": null
            }]
          }]
        }"""
    )

    with pytest.raises(ValueError, match="all golden labels are unindexed"):
        load_runs([path])
