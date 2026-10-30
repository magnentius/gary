"""Gary: AI Game Master orchestrator.

The deterministic core (models, ports, registry, resolver, fsm, context, state) imports
nothing third-party, so it can be exercised without Discord or Gemini.
Adapters live outside it.
"""

from .models import (
    CheckKind,
    CheckOutcome,
    Mutation,
    MutationKind,
    PendingCheck,
    Roll,
)
from .registry import PendingCheckRegistry
from .resolver import resolve
from .fsm import ChannelState, Director

__all__ = [
    "CheckKind",
    "CheckOutcome",
    "ChannelState",
    "Director",
    "Mutation",
    "MutationKind",
    "PendingCheck",
    "PendingCheckRegistry",
    "Roll",
    "resolve",
]
