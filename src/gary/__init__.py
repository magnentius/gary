"""Gary: AI Game Master orchestrator.

The deterministic core (models, registry, resolver, fsm, templates, context, state)
imports nothing third-party, so it can be exercised without Discord, Gemini, or Avrae.
Adapters live outside it.
"""

from .models import (
    AvraeRoll,
    CheckKind,
    CheckOutcome,
    Mutation,
    MutationKind,
    PendingCheck,
)
from .registry import PendingCheckRegistry
from .resolver import resolve
from .fsm import ChannelState, Director

__all__ = [
    "AvraeRoll",
    "CheckKind",
    "CheckOutcome",
    "ChannelState",
    "Director",
    "Mutation",
    "MutationKind",
    "PendingCheck",
    "PendingCheckRegistry",
    "resolve",
]
