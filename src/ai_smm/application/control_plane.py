"""What the control plane is allowed to do, and what it can know.

Two things live here.

**The mutation switch, at the service boundary.** The HTTP layer refuses
every business-state change centrally when
AI_SMM_API_MUTATIONS_ENABLED is off (ai_smm.api.mutation_gate). The
commands that move rows into, through or out of the publication queue --
schedule, reschedule, cancel, materialize -- check the same switch again
themselves, so a route that ever escapes the central gate still cannot
reach the queue. They take the decision as a required keyword argument:
a new caller has to state it, it cannot inherit a permissive default.

The CLI and the worker do not call these functions (they use ai_smm.queue
directly), so this check never touches the execution plane.

**The worker's execution mode, as far as the API can tell.** The API
runs in a different process, possibly on a different host, with its own
environment. Its AI_SMM_DRY_RUN says nothing about whether the worker
publishes, and nothing the worker writes today records its mode in the
database. So the only honest answer the API can give is UNKNOWN, and
that is what observed_worker_mode returns until the worker reports its
mode through a source the API can verify.
"""
from __future__ import annotations

import enum
from dataclasses import dataclass


class MutationsDisabled(Exception):
    """The control plane is read-only; the command was not attempted."""

    def __init__(self, *, command: str) -> None:
        super().__init__(
            f"Cannot {command}: the control plane is read-only "
            "(AI_SMM_API_MUTATIONS_ENABLED is off)."
        )

        self.command = command


def require_mutations_enabled(
    mutations_enabled: bool, *, command: str
) -> None:
    """Refuse before anything is read, locked or written."""

    if mutations_enabled is not True:
        raise MutationsDisabled(command=command)


class WorkerMode(str, enum.Enum):
    LIVE = "live"
    DRY_RUN = "dry_run"
    #: No trustworthy source. Never to be read as "dry run".
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class WorkerModeObservation:
    mode: WorkerMode
    #: Where the mode came from; None when it is UNKNOWN.
    source: str | None


def observed_worker_mode() -> WorkerModeObservation:
    """The worker's mode, or UNKNOWN when nothing verifiable reports it.

    Deliberately not derived from this process's settings, nor from past
    attempt outcomes: both describe something other than what the worker
    will do with the next due publication.
    """

    return WorkerModeObservation(mode=WorkerMode.UNKNOWN, source=None)
