"""Compare retrieval-eval runs across embedding truncation dimensions.

The input files are JSON artifacts produced by ``eval.run_eval``. Metrics are
recomputed from per-question diagnosis ranks so one normal run (limit=10) can
be read at both k=5 and k=10. Bootstrap confidence intervals resample paired
questions, preserving the within-question comparison between each dimension
and the 1536-dimension reference.

Usage:
    uv run python -m eval.analyze_dims eval/runs/exp8_sweep_*.json
"""

from __future__ import annotations

import argparse
import json
import math
import random
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Iterable

FULL_DIMENSIONS = 1536


@dataclass(frozen=True)
class QuestionRanks:
    question_id: str
    question: str
    final_rank: int | None
    vector_rank: int | None
    relevant_ranks: tuple[int | None, ...]


@dataclass(frozen=True)
class DimensionRun:
    path: Path
    label: str
    mode: str
    dimensions: int
    repo_name: str
    ref: str
    questions: tuple[QuestionRanks, ...]

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.repo_name, self.ref, self.mode)


def _minimum_rank(values: Iterable[int | None]) -> int | None:
    ranks = [value for value in values if value is not None]
    return min(ranks) if ranks else None


def _parse_questions(report: dict, path: Path) -> tuple[QuestionRanks, ...]:
    questions = []
    indexed_labels = 0
    seen_question_ids: set[str] = set()
    for row in report.get("per_question", []):
        question_id = str(row["id"])
        if question_id in seen_question_ids:
            raise ValueError(
                f"{path}: duplicate question id {question_id!r} in per_question"
            )
        seen_question_ids.add(question_id)

        diagnosis = row.get("diagnosis")
        if not isinstance(diagnosis, list) or not diagnosis:
            raise ValueError(
                f"{path}: question {row.get('id', '<unknown>')} has no diagnosis; "
                "rerun it with the instrumented eval harness"
            )
        indexed_labels += sum(bool(item.get("indexed")) for item in diagnosis)
        relevant_ranks = tuple(item.get("final_rank") for item in diagnosis)
        questions.append(
            QuestionRanks(
                question_id=question_id,
                question=str(row.get("question", "")),
                final_rank=row.get("first_rank", _minimum_rank(relevant_ranks)),
                vector_rank=_minimum_rank(
                    item.get("vector_rank") for item in diagnosis
                ),
                relevant_ranks=relevant_ranks,
            )
        )
    if not questions:
        raise ValueError(f"{path}: report has no per_question rows")
    if indexed_labels == 0:
        raise ValueError(
            f"{path}: all golden labels are unindexed; verify the run used the "
            "intended database, repository, and ref"
        )
    return tuple(questions)


def _parse_report(
    report: dict,
    path: Path,
    label: str,
) -> DimensionRun:
    config = report.get("config", {})
    dimensions = config.get("vector_dims") or FULL_DIMENSIONS
    if not isinstance(dimensions, int):
        raise ValueError(f"{path}: config.vector_dims must be an integer or null")
    if config.get("limit", 0) < 10:
        raise ValueError(
            f"{path}: config.limit must be at least 10 to report @10 metrics"
        )
    return DimensionRun(
        path=path,
        label=label,
        mode=str(config.get("mode", "unknown")),
        dimensions=dimensions,
        repo_name=str(report.get("repo_name", "unknown-repo")),
        ref=str(report.get("ref", "unknown-ref")),
        questions=_parse_questions(report, path),
    )


def load_runs(paths: Iterable[Path]) -> list[DimensionRun]:
    runs = []
    for path in paths:
        payload = json.loads(path.read_text())
        label = str(payload.get("label") or path.stem)
        if "ablation" in payload:
            for mode, report in payload["ablation"].items():
                runs.append(_parse_report(report, path, f"{label}:{mode}"))
        else:
            runs.append(_parse_report(payload, path, label))
    if not runs:
        raise ValueError("no run files supplied")
    return runs


def _rank_at_cutoff(rank: int | None, cutoff: int) -> int | None:
    return rank if rank is not None and rank <= cutoff else None


def metrics(run: DimensionRun, cutoff: int) -> dict[str, float]:
    hits = []
    recalls = []
    reciprocal_ranks = []
    for question in run.questions:
        ranks = [
            rank
            for rank in question.relevant_ranks
            if rank is not None and rank <= cutoff
        ]
        hits.append(1.0 if ranks else 0.0)
        recalls.append(len(ranks) / len(question.relevant_ranks))
        first_rank = _rank_at_cutoff(question.final_rank, cutoff)
        reciprocal_ranks.append(1.0 / first_rank if first_rank else 0.0)
    count = len(run.questions)
    return {
        "hit_rate": sum(hits) / count,
        "recall": sum(recalls) / count,
        "mrr": sum(reciprocal_ranks) / count,
    }


def _percentile(values: list[float], quantile: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * quantile
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (position - lower)


def paired_bootstrap_mrr_delta(
    candidate: DimensionRun,
    baseline: DimensionRun,
    cutoff: int,
    iterations: int = 10_000,
    seed: int = 8,
) -> tuple[float, float, float]:
    candidate_by_id = {q.question_id: q for q in candidate.questions}
    baseline_by_id = {q.question_id: q for q in baseline.questions}
    if candidate_by_id.keys() != baseline_by_id.keys():
        missing = sorted(baseline_by_id.keys() - candidate_by_id.keys())
        extra = sorted(candidate_by_id.keys() - baseline_by_id.keys())
        raise ValueError(
            f"cannot pair {candidate.label!r} with {baseline.label!r}: "
            f"missing ids={missing}, extra ids={extra}"
        )

    paired_deltas = []
    for question_id in baseline_by_id:
        candidate_rank = _rank_at_cutoff(
            candidate_by_id[question_id].final_rank, cutoff
        )
        baseline_rank = _rank_at_cutoff(
            baseline_by_id[question_id].final_rank, cutoff
        )
        candidate_rr = 1.0 / candidate_rank if candidate_rank else 0.0
        baseline_rr = 1.0 / baseline_rank if baseline_rank else 0.0
        paired_deltas.append(candidate_rr - baseline_rr)

    observed = sum(paired_deltas) / len(paired_deltas)
    rng = random.Random(seed)
    bootstrapped = [
        sum(rng.choice(paired_deltas) for _ in paired_deltas) / len(paired_deltas)
        for _ in range(iterations)
    ]
    return (
        observed,
        _percentile(bootstrapped, 0.025),
        _percentile(bootstrapped, 0.975),
    )


def _corpus(run: DimensionRun) -> str:
    return f"{run.repo_name}@{run.ref}"


def _preferred(candidates: list[DimensionRun]) -> DimensionRun:
    ordered = sorted(
        candidates,
        key=lambda run: (0 if "sweep" in run.label else 1, run.label, str(run.path)),
    )
    return ordered[0]


def pool_runs(runs: list[DimensionRun]) -> dict[str, list[DimensionRun]]:
    """Pool paired per-question rows across corpora, one pooled run per dim.

    Question IDs are namespaced by ``repo@ref`` so each corpus's ``q01`` stays
    distinct, keeping the bootstrap pairing per-question rather than averaging
    per-corpus aggregates. A dim is pooled only when every corpus in the mode
    has a run at it; with several candidate runs (e.g. sweep plus jitter
    baselines) the ``sweep``-labeled one is preferred deterministically.
    """
    by_mode: dict[str, dict[str, dict[int, list[DimensionRun]]]] = {}
    for run in runs:
        by_corpus = by_mode.setdefault(run.mode, {})
        by_corpus.setdefault(_corpus(run), {}).setdefault(
            run.dimensions, []
        ).append(run)

    pooled_by_mode: dict[str, list[DimensionRun]] = {}
    for mode, by_corpus in sorted(by_mode.items()):
        if len(by_corpus) < 2:
            continue
        shared_dims = set.intersection(
            *(set(dims) for dims in by_corpus.values())
        )
        if FULL_DIMENSIONS not in shared_dims:
            continue
        pooled_runs_for_mode = []
        for dim in sorted(shared_dims, reverse=True):
            questions = []
            source_labels = []
            for corpus, dims in sorted(by_corpus.items()):
                chosen = _preferred(dims[dim])
                source_labels.append(chosen.label)
                questions.extend(
                    replace(
                        question,
                        question_id=f"{corpus}:{question.question_id}",
                    )
                    for question in chosen.questions
                )
            pooled_runs_for_mode.append(
                DimensionRun(
                    path=Path("<pooled>"),
                    label=f"pooled:{dim}",
                    mode=mode,
                    dimensions=dim,
                    repo_name="pooled",
                    ref=" + ".join(source_labels),
                    questions=tuple(questions),
                )
            )
        pooled_by_mode[mode] = pooled_runs_for_mode
    return pooled_by_mode


def _print_pooled(
    runs: list[DimensionRun],
    iterations: int,
    seed: int,
) -> None:
    for mode, group in pool_runs(runs).items():
        pooled_dims = {run.dimensions for run in group}
        skipped = sorted(
            {run.dimensions for run in runs if run.mode == mode} - pooled_dims,
            reverse=True,
        )
        question_count = len(group[0].questions)
        print(
            f"\nPOOLED ANALYSIS | mode={mode} | "
            f"{question_count} paired questions"
        )
        print("=" * 101)
        for run in group:
            print(f"  {run.label}: {run.ref}")
        if skipped:
            print(
                "  dims skipped (missing from at least one corpus): "
                + ", ".join(str(dim) for dim in skipped)
            )
        _print_metrics(group)
        _print_bootstrap(group, iterations, seed)


def _fmt_rank(rank: int | None) -> str:
    return str(rank) if rank is not None else "—"


def _run_heading(run: DimensionRun) -> str:
    return f"{run.dimensions}:{run.label}"


def _print_trajectory(
    title: str,
    runs: list[DimensionRun],
    rank_name: str,
) -> None:
    by_run = [
        {question.question_id: question for question in run.questions}
        for run in runs
    ]
    question_ids = [question.question_id for question in runs[0].questions]
    width = max(12, *(len(_run_heading(run)) + 1 for run in runs))
    print(f"\n{title}")
    print(f"{'id':<8}" + "".join(f"{_run_heading(run):>{width}}" for run in runs))
    print("-" * (8 + width * len(runs)))
    for question_id in question_ids:
        cells = []
        for questions in by_run:
            if question_id not in questions:
                cells.append("missing")
            else:
                cells.append(_fmt_rank(getattr(questions[question_id], rank_name)))
        print(f"{question_id:<8}" + "".join(f"{cell:>{width}}" for cell in cells))


def _print_metrics(runs: list[DimensionRun]) -> None:
    print("\nCUTOFF METRICS (derived from diagnosis.final_rank)")
    print(
        f"{'dims':>6}  {'label':<24}"
        f"{'hit@5':>9}{'recall@5':>11}{'MRR@5':>9}"
        f"{'hit@10':>10}{'recall@10':>12}{'MRR@10':>10}"
    )
    print("-" * 101)
    for run in runs:
        at_5 = metrics(run, 5)
        at_10 = metrics(run, 10)
        print(
            f"{run.dimensions:>6}  {run.label:<24}"
            f"{at_5['hit_rate']:>9.3f}{at_5['recall']:>11.3f}{at_5['mrr']:>9.3f}"
            f"{at_10['hit_rate']:>10.3f}{at_10['recall']:>12.3f}"
            f"{at_10['mrr']:>10.3f}"
        )


def _reference_run(runs: list[DimensionRun]) -> DimensionRun:
    baselines = [run for run in runs if run.dimensions == FULL_DIMENSIONS]
    if not baselines:
        raise ValueError(
            f"mode {runs[0].mode!r} has no {FULL_DIMENSIONS}-dimension baseline"
        )
    sweep_baselines = [run for run in baselines if "sweep" in run.label]
    return (sweep_baselines or baselines)[0]


def _print_bootstrap(
    runs: list[DimensionRun],
    iterations: int,
    seed: int,
) -> None:
    baseline = _reference_run(runs)
    print(
        f"\nPAIRED BOOTSTRAP MRR DELTA VS {baseline.label} "
        f"({iterations:,} iterations, 95% CI)"
    )
    print(
        f"{'dims':>6}  {'label':<24}"
        f"{'delta@5':>10}{'95% CI @5':>24}"
        f"{'delta@10':>11}{'95% CI @10':>24}"
    )
    print("-" * 101)
    for run in runs:
        if run is baseline:
            delta_5 = low_5 = high_5 = 0.0
            delta_10 = low_10 = high_10 = 0.0
        else:
            delta_5, low_5, high_5 = paired_bootstrap_mrr_delta(
                run, baseline, 5, iterations, seed
            )
            delta_10, low_10, high_10 = paired_bootstrap_mrr_delta(
                run, baseline, 10, iterations, seed
            )
        ci_5 = f"[{low_5:+.3f}, {high_5:+.3f}]"
        ci_10 = f"[{low_10:+.3f}, {high_10:+.3f}]"
        print(
            f"{run.dimensions:>6}  {run.label:<24}"
            f"{delta_5:>+10.3f}{ci_5:>24}"
            f"{delta_10:>+11.3f}{ci_10:>24}"
        )


def print_analysis(
    runs: list[DimensionRun],
    iterations: int = 10_000,
    seed: int = 8,
) -> None:
    grouped: dict[tuple[str, str, str], list[DimensionRun]] = {}
    for run in runs:
        grouped.setdefault(run.key, []).append(run)

    for group_number, ((repo_name, ref, mode), group) in enumerate(
        sorted(grouped.items()), 1
    ):
        if group_number > 1:
            print()
        group.sort(key=lambda run: (-run.dimensions, run.label, str(run.path)))
        print(
            f"DIMENSION ANALYSIS | repo={repo_name} | ref={ref} | mode={mode}"
        )
        print("=" * 101)
        _print_trajectory("FINAL FIRST-RANK TRAJECTORY", group, "final_rank")
        _print_trajectory("RAW VECTOR FIRST-RANK TRAJECTORY", group, "vector_rank")
        _print_metrics(group)
        _print_bootstrap(group, iterations, seed)

    _print_pooled(runs, iterations, seed)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("runs", nargs="+", type=Path, help="run JSON files")
    parser.add_argument(
        "--iterations",
        type=int,
        default=10_000,
        help="paired bootstrap iterations (default: 10000)",
    )
    parser.add_argument(
        "--seed",
        type=int,
        default=8,
        help="bootstrap random seed (default: 8)",
    )
    args = parser.parse_args()
    if args.iterations < 1:
        parser.error("--iterations must be positive")
    try:
        print_analysis(load_runs(args.runs), args.iterations, args.seed)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
