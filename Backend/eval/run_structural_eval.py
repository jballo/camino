"""Structural eval for tour artifacts — schema + repo grounding, no LLM.

Runs hand-written fixtures through ``validate_tour`` against the pinned FastAPI
fixture repo (same source tree as the retrieval golden set).

Usage:
    uv run python -m eval.run_structural_eval
    uv run python -m eval.run_structural_eval --json
    uv run python -m eval.run_structural_eval --fixture valid_minimal
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from eval.ingest_local import (
    DEFAULT_FIXTURE_PATH,
    FIXTURE_REPO_URL,
    FIXTURE_REPO_VERSION,
    ensure_fixture,
)
from eval.structural.validate import CheckKind, ValidationResult, validate_tour

MANIFEST_PATH = Path(__file__).parent / "structural" / "fixtures" / "manifest.json"
FIXTURES_DIR = MANIFEST_PATH.parent


@dataclass
class FixtureExpectation:
    id: str
    file: str
    expect: str
    fail_checks: list[str]


@dataclass
class FixtureRun:
    id: str
    expect: str
    passed: bool
    expected_pass: bool
    failed_checks: list[str]
    expected_fail_checks: list[str]
    issues: list[dict]


def _load_manifest() -> tuple[str, list[FixtureExpectation]]:
    data = json.loads(MANIFEST_PATH.read_text())
    fixtures = [
        FixtureExpectation(
            id=f["id"],
            file=f["file"],
            expect=f["expect"],
            fail_checks=f.get("fail_checks", []),
        )
        for f in data["fixtures"]
    ]
    return data.get("repo_version", FIXTURE_REPO_VERSION), fixtures


def _run_fixture(
    spec: FixtureExpectation,
    repo_root: Path,
) -> FixtureRun:
    payload = (FIXTURES_DIR / spec.file).read_text()
    result = validate_tour(payload, repo_root)
    expected_pass = spec.expect == "pass"
    failed = sorted(result.failed_checks)
    expected_failed = sorted(CheckKind(c) for c in spec.fail_checks)
    passed = result.passed == expected_pass and failed == expected_failed
    return FixtureRun(
        id=spec.id,
        expect=spec.expect,
        passed=passed,
        expected_pass=expected_pass,
        failed_checks=[c.value for c in failed],
        expected_fail_checks=spec.fail_checks,
        issues=[
            {
                "kind": issue.kind.value,
                "step_index": issue.step_index,
                "message": issue.message,
            }
            for issue in result.issues
        ],
    )


def _aggregate(runs: list[FixtureRun]) -> dict:
    n = len(runs)
    harness_pass = sum(1 for r in runs if r.passed)
    artifact_pass = sum(1 for r in runs if r.expected_pass)
    return {
        "fixtures": n,
        "harness_pass_rate": harness_pass / n if n else 0.0,
        "artifact_pass_rate": artifact_pass / n if n else 0.0,
        "harness_pass": harness_pass,
        "artifact_pass": artifact_pass,
    }


def _print_report(
    repo_root: Path,
    repo_version: str,
    runs: list[FixtureRun],
    aggregate: dict,
) -> None:
    print(f"\nStructural eval | repo_root={repo_root} | version={repo_version}")
    print("=" * 78)
    print(f"{'id':<16}{'expect':>8}{'ok':>4}  failed_checks")
    print("-" * 78)
    for run in runs:
        ok = "Y" if run.passed else "N"
        failed = ",".join(run.failed_checks) or "—"
        print(f"{run.id:<16}{run.expect:>8}{ok:>4}  {failed}")
    print("=" * 78)
    print("AGGREGATE")
    print(f"  fixtures          : {aggregate['fixtures']}")
    print(f"  harness_pass      : {aggregate['harness_pass']}/{aggregate['fixtures']}")
    print(f"  artifact_pass     : {aggregate['artifact_pass']}/{aggregate['fixtures']}")
    print()


def _print_failures(runs: list[FixtureRun]) -> None:
    mismatches = [r for r in runs if not r.passed]
    if not mismatches:
        return
    print(f"MISMATCHES ({len(mismatches)})")
    for run in mismatches:
        print(f"  [{run.id}] expect={run.expect!r} failed={run.failed_checks!r} "
              f"expected_fail={run.expected_fail_checks!r}")
        for issue in run.issues:
            step = issue["step_index"]
            step_label = f"step {step}" if step is not None else "tour"
            print(f"        {issue['kind']} ({step_label}): {issue['message']}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(DEFAULT_FIXTURE_PATH),
        help="Local checkout of the fixture repo (default: eval/.data/fastapi)",
    )
    parser.add_argument(
        "--no-clone",
        action="store_true",
        help="Do not auto-clone the pinned fixture if the path is missing",
    )
    parser.add_argument(
        "--fixture",
        action="append",
        dest="fixtures",
        help="Run only these fixture ids (repeatable)",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    parser.add_argument(
        "--out",
        default=None,
        help="write the full report JSON to this path",
    )
    args = parser.parse_args()

    repo_root = Path(args.repo_root)
    if not args.no_clone:
        ensure_fixture(repo_root, FIXTURE_REPO_URL, FIXTURE_REPO_VERSION)
    repo_root = repo_root.resolve()
    if not repo_root.exists():
        raise SystemExit(
            f"repo root does not exist: {repo_root} "
            "(omit --no-clone to auto-fetch the fixture)"
        )

    repo_version, manifest = _load_manifest()
    if args.fixtures:
        wanted = set(args.fixtures)
        manifest = [spec for spec in manifest if spec.id in wanted]
        missing = wanted - {spec.id for spec in manifest}
        if missing:
            raise SystemExit(f"unknown fixture id(s): {', '.join(sorted(missing))}")

    runs = [_run_fixture(spec, repo_root) for spec in manifest]
    aggregate = _aggregate(runs)
    output = {
        "repo_root": str(repo_root),
        "repo_version": repo_version,
        "aggregate": aggregate,
        "fixtures": [asdict(run) for run in runs],
    }

    if args.json:
        print(json.dumps(output, indent=2))
    else:
        _print_report(repo_root, repo_version, runs, aggregate)
        _print_failures(runs)

    if args.out:
        out_path = Path(args.out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(output, indent=2))
        print(f"wrote {out_path}")

    if aggregate["harness_pass"] != aggregate["fixtures"]:
        sys.exit(1)


if __name__ == "__main__":
    main()
