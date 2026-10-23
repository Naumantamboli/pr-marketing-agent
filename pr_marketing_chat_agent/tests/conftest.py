import logging
import os
from datetime import UTC, datetime, timedelta

import pytest

os.environ.setdefault("LOCAL_OUTPUT", "true")


@pytest.fixture
def local_output(tmp_path, monkeypatch):
    """Route storage to a temp dir."""
    from utils import storage

    monkeypatch.setenv("LOCAL_OUTPUT", "true")
    monkeypatch.setattr(storage, "_LOCAL_DIR", tmp_path)
    return tmp_path


class Clock:
    def __init__(self) -> None:
        self.t = datetime(2026, 10, 22, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, minutes: float) -> None:
        self.t += timedelta(minutes=minutes)


@pytest.fixture
def wf(monkeypatch):
    """The workflow module with Temporal's clock and run id replaced, outside a workflow."""
    from agent import workflow

    clock = Clock()
    monkeypatch.setattr(workflow, "_now", clock)
    monkeypatch.setattr(workflow, "_run_id", lambda: "run-1")
    # Temporal's workflow logger only works inside a workflow event loop.
    monkeypatch.setattr(workflow.workflow, "logger", logging.getLogger("workflow"))
    workflow.clock = clock
    return workflow
