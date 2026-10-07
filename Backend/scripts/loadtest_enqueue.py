"""Enqueue repository-ingest jobs directly, bypassing the API.

Load-test helper: creates the same rows ``POST /repositories/ingest`` would,
without needing a Clerk session. Run from ``Backend/`` under Doppler so
Settings finds the environment (there are no .env files):

    doppler run -- uv run python -m scripts.loadtest_enqueue \
        --user-id user_xxx --installation-id 12345 \
        --repo owner/name --repo owner/other@main

A repo may pin a ref with ``owner/name@ref``; otherwise the ref is resolved
from GitHub the same way the endpoint does. Jobs dedupe on (repo, ref), so
pass distinct repos to get distinct jobs.

Each repo gets a shared ingest plus this user's waiting row on it; workers
only run a shared ingest while someone is waiting. The last line of output is
machine-readable: ``JOB_IDS=<id> <id> ...``, the shared ingest IDs, which are
the rows that actually run.
"""

import argparse
import sys

from sqlmodel import Session, select

from app.db import engine
from app.models.github_connection import GithubConnections
from app.services.shared_ingests import enqueue_shared_ingest
from app.services.target_branch import resolve_target_branch


def ensure_installation_connection(
    session: Session, *, user_id: str, installation_id: int
) -> None:
    """Seed the githubconnections row the worker's ingestion guard requires.

    Workers refuse to claim or commit a shared ingest unless a waiting user has
    an active connection for the waiting row's installation. Production rows
    come from the GitHub-app connect flow; a throwaway load-test database has
    none, so seed a placeholder. Ingestion mints installation tokens from the app credentials
    and never reads this row's token fields.
    """
    existing = session.exec(
        select(GithubConnections).where(
            GithubConnections.userId == user_id,
            GithubConnections.installationId == installation_id,
        )
    ).first()
    if existing is not None:
        if not existing.active:
            existing.active = True
            session.add(existing)
            session.commit()
        return
    conflict = session.exec(
        select(GithubConnections).where(GithubConnections.userId == user_id)
    ).first()
    if conflict:
        print(
            f"user {user_id} is connected to installation "
            f"{conflict.installationId}, not {installation_id}; workers would "
            "abandon these jobs — fix --installation-id",
            file=sys.stderr,
        )
        raise SystemExit(1)
    session.add(
        GithubConnections(
            userId=user_id,
            githubUsername="loadtest-placeholder",
            githubUserId=0,
            installationId=installation_id,
        )
    )
    session.commit()
    print(
        f"seeded placeholder githubconnections row for installation "
        f"{installation_id}"
    )


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

    # Resolve every ref before enqueueing anything: each enqueue commits, so
    # failing mid-loop would leave committed jobs missing from JOB_IDS.
    targets: list[tuple[str, str]] = []
    for spec in args.repos:
        repo_name, _, ref = spec.partition("@")
        if not ref:
            ref = resolve_target_branch(repo_name, args.installation_id).branch
            if not ref:
                print(f"could not resolve ref for {repo_name}", file=sys.stderr)
                return 1
        targets.append((repo_name, ref))

    job_ids: list[int] = []
    with Session(engine) as session:
        ensure_installation_connection(
            session, user_id=args.user_id, installation_id=args.installation_id
        )
        for repo_name, ref in targets:
            _, shared, created = enqueue_shared_ingest(
                session,
                user_id=args.user_id,
                installation_id=args.installation_id,
                repo_name=repo_name,
                ref=ref,
            )
            print(
                f"job {shared.id} {'queued' if created else 'deduplicated (already active)'}"
                f" | {repo_name}@{ref}"
            )
            job_ids.append(shared.id)

    print("JOB_IDS=" + " ".join(str(i) for i in job_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
