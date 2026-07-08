"""Shared state threaded through the tour generation graph."""

from typing import TypedDict

from app.models.tour import TourStep
from app.services.search import SearchResult
from app.tour.schemas import PlannedStep


class TourState(TypedDict, total=False):
    """State for the Plan -> Retrieve -> Draft pipeline.

    ``total=False`` so the initial invocation only has to supply the inputs
    (``topic``, ``repo_name``, ``installation_id``); each node fills in the keys
    it produces. Later keys overwrite (no reducers needed for a linear graph).
    """

    # inputs
    topic: str
    repo_name: str
    installation_id: int

    # produced by nodes
    title: str
    plan: list[PlannedStep]
    candidates: dict[int, list[SearchResult]]
    steps: list[TourStep]
