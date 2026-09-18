"""Enqueue repository-ingest jobs directly, bypassing the API.

Load-test helper: creates the same rows ``POST /repositories/ingest`` would,
without needing a Clerk session. Run from ``Backend/`` so Settings finds .env:

    uv run python -m scripts.loadtest_enqueue \
        --user-id user_xxx --installation-id 12345 \
        --repo owner/name --repo owner/other@main

A repo may pin a ref with ``owner/name@ref``; otherwise the ref is resolved
from GitHub the same way the endpoint does. Jobs dedupe on (repo, ref), so
pass distinct repos to get distinct jobs.

The last line of output is machine-readable: ``JOB_IDS=<id> <id> ...``.
"""

import argparse
import sys

from sqlmodel import Session

from app.db import engine
from app.models.job import JobType
from app.services.jobs import enqueue_job, repository_ingest_dedupe_key
from app.services.target_branch import resolve_target_branch


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", required=True, help="Clerk user id to own the jobs")
    parser.add_argument(
        "--installation-id",
        required=True,
        type=int,
        help="GitHub App installation id used to fetch the repos",
    )
    parser.add_argument(
        "--repo",
        action="append",
        required=True,
        dest="repos",
        help="owner/name or owner/name@ref; repeat for each job",
    )
    args = parser.parse_args()

    job_ids: list[int] = []
    with Session(engine) as session:
        for spec in args.repos:
            repo_name, _, ref = spec.partition("@")
            if not ref:
                ref = resolve_target_branch(repo_name, args.installation_id).branch
                if not ref:
                    print(f"could not resolve ref for {repo_name}", file=sys.stderr)
                    return 1
            job, created = enqueue_job(
                session,
                user_id=args.user_id,
                installation_id=args.installation_id,
                repo_name=repo_name,
                ref=ref,
                job_type=JobType.REPOSITORY_INGEST,
                dedupe_key=repository_ingest_dedupe_key(repo_name=repo_name, ref=ref),
            )
            print(
                f"job {job.id} {'queued' if created else 'deduplicated (already active)'}"
                f" | {repo_name}@{ref}"
            )
            job_ids.append(job.id)

    print("JOB_IDS=" + " ".join(str(i) for i in job_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
