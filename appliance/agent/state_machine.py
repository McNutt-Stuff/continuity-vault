"""
Appliance local state machine (spec 4.2).

The appliance enforces its own state transitions. State is authoritative locally;
the cloud only observes it via signed heartbeats. Protected storage is accessible
only in explicitly unsealed states, and never to the management controller.
"""

from __future__ import annotations

from enum import Enum
from typing import Set


class State(str, Enum):
    PROVISIONING = "PROVISIONING"
    ONLINE_STAGING = "ONLINE_STAGING"
    READY_TO_SEAL = "READY_TO_SEAL"
    SEALING = "SEALING"
    SEALED = "SEALED"
    UNSEAL_REQUESTED = "UNSEAL_REQUESTED"
    UNSEALED_FOR_INGEST = "UNSEALED_FOR_INGEST"
    UNSEALED_FOR_RECOVERY = "UNSEALED_FOR_RECOVERY"
    VERIFYING = "VERIFYING"
    MAINTENANCE = "MAINTENANCE"
    QUARANTINED = "QUARANTINED"
    DECOMMISSIONING = "DECOMMISSIONING"
    DESTROYED = "DESTROYED"


# Allowed transitions (spec 4.2 state machine).
_TRANSITIONS: dict[State, Set[State]] = {
    State.PROVISIONING: {State.ONLINE_STAGING},
    State.ONLINE_STAGING: {State.READY_TO_SEAL, State.UNSEALED_FOR_INGEST,
                           State.MAINTENANCE, State.QUARANTINED},
    State.READY_TO_SEAL: {State.SEALING, State.ONLINE_STAGING},
    State.SEALING: {State.SEALED},
    State.SEALED: {State.UNSEAL_REQUESTED, State.MAINTENANCE, State.QUARANTINED,
                   State.VERIFYING, State.DECOMMISSIONING},
    State.UNSEAL_REQUESTED: {State.UNSEALED_FOR_INGEST, State.UNSEALED_FOR_RECOVERY,
                             State.SEALED, State.QUARANTINED},
    State.UNSEALED_FOR_INGEST: {State.SEALING, State.QUARANTINED},
    State.UNSEALED_FOR_RECOVERY: {State.SEALING, State.QUARANTINED},
    State.VERIFYING: {State.SEALED, State.QUARANTINED},
    State.MAINTENANCE: {State.ONLINE_STAGING, State.SEALED, State.QUARANTINED},
    State.QUARANTINED: {State.MAINTENANCE, State.SEALED},
    State.DECOMMISSIONING: {State.DESTROYED},
    State.DESTROYED: set(),
}

# States in which the protected storage data path may be opened.
STORAGE_ACCESSIBLE = {State.UNSEALED_FOR_INGEST, State.UNSEALED_FOR_RECOVERY}


class StateMachine:
    def __init__(self, initial: State = State.PROVISIONING) -> None:
        self.state = initial

    def can_transition(self, target: State) -> bool:
        return target in _TRANSITIONS.get(self.state, set())

    def transition(self, target: State) -> None:
        if not self.can_transition(target):
            raise ValueError(f"illegal transition {self.state} -> {target}")
        self.state = target

    @property
    def storage_accessible(self) -> bool:
        return self.state in STORAGE_ACCESSIBLE

    @property
    def isolation_state(self) -> str:
        return "open" if self.storage_accessible else "sealed"
