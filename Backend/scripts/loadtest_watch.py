"""Watch load-test jobs until they all finish, then print a timing summary.

Run from ``Backend/`` under Doppler so the database URL is in the environment
(there are no .env files):

    doppler run -- uv run python -m scripts.loadtest_watch 12 13 14 15 16

Polls the jobs table directly, logs every status transition as it happens,
and reports per-job queue-wait and run duration plus the peak number of
concurrently running jobs.
"""

import argparse
import datetime as dt
import time

from sqlmodel import Session, select

from app.db import engine
from app.models.job import Job, JobStatus

TERMINAL = {JobStatus.COMPLETE, JobStatus.FAILED, JobStatus.CANCELLED}


def _fmt(seconds: float | None) -> str:
    return "-" if seconds is None else f"{seconds:7.1f}s"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("job_ids", nargs="+", type=int)
    parser.add_argument("--interval", type=float, default=2.0)
    args = parser.parse_args()

    last_status: dict[int, str] = {}
    # the worker nulls claimed_at when it releases a finished job, so snapshot
    # it while the job is still running or the final table has nothing to show
    claimed: dict[int, dt.datetime] = {}
    peak_running = 0
    while True:
        with Session(engine) as session:
            jobs = session.exec(select(Job).where(Job.id.in_(args.job_ids))).all()
        by_id = {job.id: job for job in jobs}
        now = dt.datetime.now().strftime("%H:%M:%S")
        for job_id in args.job_ids:
            job = by_id.get(job_id)
            if job and job.claimed_at and job_id not in claimed:
                claimed[job_id] = job.claimed_at
            status = job.status if job else "missing"
            if last_status.get(job_id) != status:
                print(
                    f"{now} job {job_id}: {last_status.get(job_id, '?')} -> {status}",
                    flush=True,
                )
                last_status[job_id] = status
        running = sum(1 for j in jobs if j.status == JobStatus.RUNNING)
        peak_running = max(peak_running, running)
        # a job deleted mid-watch (e.g. a between-run TRUNCATE) counts as
        # terminal, or the watcher would poll forever waiting for it
        if all(
            job_id not in by_id or by_id[job_id].status in TERMINAL
            for job_id in args.job_ids
        ):
            break
        time.sleep(args.interval)

    print(f"\npeak concurrent running: {peak_running}")
    print(f"{'job':>5} {'status':<10} {'queue wait':>10} {'run time':>10}  repo")
    for job_id in args.job_ids:
        job = by_id.get(job_id)
        if job is None:
            print(f"{job_id:>5} {'missing':<10} {_fmt(None)} {_fmt(None)}")
            continue
        claimed_at = job.claimed_at or claimed.get(job_id)
        wait = (
            (claimed_at - job.createdAt).total_seconds() if claimed_at else None
        )
        run = (
            (job.updatedAt - claimed_at).total_seconds() if claimed_at else None
        )
        print(
            f"{job.id:>5} {job.status:<10} {_fmt(wait)} {_fmt(run)}"
            f"  {job.repo_name}@{job.ref}"
            + (f"  error: {job.error}" if job.error else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
