"""Firestore backends — the disposable-VM setup (ARCHITECTURE.md §11).

Point ``CampaignStore`` and ``PendingCheckRegistry`` at these and the VM owns nothing.
Delete the instance, recreate it, and the campaign is intact.

    pip install -e '.[gcp]'

    from google.cloud import firestore
    from gary.state.firestore import FirestoreStateBackend, FirestoreCheckBackend

    db = firestore.Client()
    store = CampaignStore(FirestoreStateBackend(db, "my-campaign"), clock=time.time)
    registry = PendingCheckRegistry(clock=time.time,
                                    backend=FirestoreCheckBackend(db, "my-campaign"))

**Verified against the emulator, 2026-08-20** (`google-cloud-firestore` 2.28.1). The
shared backend suite in ``tests/test_backend.py`` passes against real Firestore semantics,
not only the fake client -- journal ordering with sequences written out of order, the
missing-``seq`` refusal, snapshot overwrite, and loading a campaign that does not exist.
To re-run it, start the emulator and substitute a real client for ``FakeFirestoreClient``:

    gcloud emulators firestore start --host-port=localhost:8080   # needs a JDK
    export FIRESTORE_EMULATOR_HOST=localhost:8080

Data model
----------
``campaigns/{id}``                  snapshot document
``campaigns/{id}/journal/{seq}``    one document per journal entry
``campaigns/{id}/checks/{check_id}``  one document per pending check

State and journal entries are stored as **JSON strings in a single field**, not as native
Firestore maps. This is deliberate:

* Firestore map keys are restricted; character names ("Mr. Blackwood") and item names
  routinely contain characters that make nested field paths ambiguous.
* Firestore coerces numeric types on the way in and out. The journal must replay to
  exactly what was written.

Nothing here needs to be queried server-side — state is always loaded whole — so the only
thing the map representation would buy is a class of silent corruption bugs.

Known limit
-----------
A Firestore document is capped at 1 MiB. The snapshot holds ``notes``, which grows for the
life of the campaign. At roughly 200 bytes a note that is ~5000 notes, which a weekly game
will not reach for years — but it is a real ceiling, not a theoretical one. When it
approaches, move ``notes`` into its own subcollection; nothing outside this file and
``context.render_memory`` depends on where they live.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, List, Optional, Tuple

from ..models import PendingCheck, restorable

log = logging.getLogger("gary.firestore")

# Measured against the emulator 2026-08-20: a write of ~1100 KiB is refused with
# 'InvalidArgument: 400 The value of property "state_json" is longer than 1048487 bytes'.
# The cap is on the field value, not the document, so this is the number that matters.
MAX_FIELD_BYTES = 1048487
WARN_AT_FRACTION = 0.7

SNAPSHOT_FIELD = "state_json"
OFFSET_FIELD = "journal_offset"
ENTRY_FIELD = "entry_json"
SEQ_FIELD = "seq"


class FirestoreStateBackend:
    """Snapshot document plus a journal subcollection.

    ``client`` is any object with Firestore's ``collection()`` surface, so a fake can be
    substituted in tests.
    """

    def __init__(self, client: Any, campaign_id: str, root_collection: str = "campaigns") -> None:
        self._doc = client.collection(root_collection).document(campaign_id)
        self._journal = self._doc.collection("journal")

    # -- snapshot ----------------------------------------------------------------------

    def load_snapshot(self) -> Optional[Tuple[Dict[str, Any], int]]:
        snap = self._doc.get()
        if not snap.exists:
            return None
        data = snap.to_dict() or {}
        raw = data.get(SNAPSHOT_FIELD)
        if raw is None:
            return None
        return json.loads(raw), int(data.get(OFFSET_FIELD, 0))

    def write_snapshot(self, state: Dict[str, Any], journal_offset: int) -> None:
        # A single set() is atomic at the document level, which is the same guarantee
        # temp-then-rename gives on local disk.
        payload = json.dumps(state, sort_keys=True)
        self._warn_if_near_cap(payload)
        self._doc.set(
            {SNAPSHOT_FIELD: payload, OFFSET_FIELD: int(journal_offset)}
        )

    @staticmethod
    def _warn_if_near_cap(payload: str) -> None:
        """Announce the ceiling before it is hit, because hitting it is unrecoverable in
        the moment.

        Nothing catches the InvalidArgument that Firestore raises past the cap, so it
        surfaces mid-turn as an opaque Google error -- and ``CampaignStore.snapshot``
        raises before resetting its counter, so every later mutation raises too and the
        campaign is write-blocked until someone shrinks the state by hand. A warning
        several sessions ahead is the difference between a scheduled migration and a
        session ending on a stack trace.
        """
        size = len(payload.encode("utf-8"))
        if size < int(MAX_FIELD_BYTES * WARN_AT_FRACTION):
            return
        log.warning(
            "campaign snapshot is %d bytes, %.0f%% of Firestore's %d-byte field cap. "
            "Past the cap every mutation fails and the campaign is write-blocked. "
            "'notes' is the growing field (~85%% of state by then); move it to a "
            "subcollection or prune it. See ARCHITECTURE.md §7.4.",
            size, 100 * size / MAX_FIELD_BYTES, MAX_FIELD_BYTES,
        )

    # -- journal -----------------------------------------------------------------------

    def append_journal(self, entry: Dict[str, Any]) -> None:
        if SEQ_FIELD not in entry:
            # Defaulting would put every entry on the same document id, each silently
            # overwriting the last. Refuse instead: losing journal history is
            # unrecoverable, and it fails invisibly until someone replays.
            raise ValueError(
                "journal entry has no 'seq'; document-based backends need it to order "
                "and address entries (see StateBackend.append_journal)"
            )
        seq = int(entry[SEQ_FIELD])
        # Zero-padded id keeps document-id order aligned with sequence order, so the
        # collection reads correctly even without an explicit order_by.
        self._journal.document(f"{seq:012d}").set(
            {SEQ_FIELD: seq, ENTRY_FIELD: json.dumps(entry, sort_keys=True)}
        )

    def read_journal(self) -> List[Dict[str, Any]]:
        docs = self._journal.order_by(SEQ_FIELD).stream()
        out: List[Dict[str, Any]] = []
        for doc in docs:
            data = doc.to_dict() or {}
            raw = data.get(ENTRY_FIELD)
            if raw is not None:
                out.append(json.loads(raw))
        return out


    def truncate_journal(self) -> None:
        # No collection-level delete exists in Firestore: a subcollection is emptied one
        # document at a time. The set is bounded by a single campaign's history, and this
        # runs once per reset, so it is not batched.
        for doc in self._journal.stream():
            self._journal.document(doc.id).delete()


class FirestoreCheckBackend:
    """One document per pending check.

    The set is tiny (bounded by players with a check open), so there is no batching and
    no pagination here on purpose.
    """

    def __init__(self, client: Any, campaign_id: str, root_collection: str = "campaigns") -> None:
        self._checks = (
            client.collection(root_collection).document(campaign_id).collection("checks")
        )

    def save(self, check: PendingCheck) -> None:
        self._checks.document(check.check_id).set(
            {"check_json": json.dumps(check.to_dict(), sort_keys=True)}
        )

    def remove(self, check_id: str) -> None:
        self._checks.document(check_id).delete()

    def load_all(self) -> List[PendingCheck]:
        raws = []
        for doc in self._checks.stream():
            data = doc.to_dict() or {}
            raw = data.get("check_json")
            if raw is not None:
                raws.append(json.loads(raw))
        return restorable(raws)
