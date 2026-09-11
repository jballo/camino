"""Backward-compatible imports for code that still uses the old tour-job names."""

from app.models.job import Job, JobStatus

TourJob = Job
TourJobStatus = JobStatus

__all__ = ["TourJob", "TourJobStatus"]
