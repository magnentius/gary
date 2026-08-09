from __future__ import annotations

import json

import pytest

from drelm.fsm import Director
from drelm.registry import PendingCheckRegistry


class FakeClock:
    """Injected everywhere time matters, so expiry is tested without sleeping."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def registry(clock: FakeClock) -> PendingCheckRegistry:
    return PendingCheckRegistry(clock=clock, ttl_seconds=180.0)


CHANNEL = 555
BOT = 999


@pytest.fixture
def director(registry: PendingCheckRegistry, clock: FakeClock) -> Director:
    return Director(
        registry=registry, clock=clock, bot_user_id=BOT, game_channels={CHANNEL}
    )


class _FakeSnapshot:
    def __init__(self, doc_id: str, data):
        self.id = doc_id
        self._data = data

    @property
    def exists(self) -> bool:
        return self._data is not None

    def to_dict(self):
        return json.loads(json.dumps(self._data)) if self._data is not None else None


class _FakeDoc:
    def __init__(self, store: dict, path: str):
        self._store = store
        self._path = path

    def get(self) -> _FakeSnapshot:
        return _FakeSnapshot(self._path.rsplit("/", 1)[-1], self._store.get(self._path))

    def set(self, data) -> None:
        # Deep copy on the way in, exactly as a real datastore would: callers must not be
        # able to mutate stored documents through the reference they passed.
        self._store[self._path] = json.loads(json.dumps(data))

    def delete(self) -> None:
        self._store.pop(self._path, None)

    def collection(self, name: str) -> "_FakeCollection":
        return _FakeCollection(self._store, f"{self._path}/{name}")


class _FakeQuery:
    def __init__(self, snapshots, field: str):
        self._snapshots = snapshots
        self._field = field

    def stream(self):
        return iter(
            sorted(self._snapshots, key=lambda s: (s.to_dict() or {}).get(self._field, 0))
        )


class _FakeCollection:
    def __init__(self, store: dict, prefix: str):
        self._store = store
        self._prefix = prefix

    def document(self, doc_id: str) -> _FakeDoc:
        return _FakeDoc(self._store, f"{self._prefix}/{doc_id}")

    def _snapshots(self):
        depth = self._prefix.count("/") + 1
        return [
            _FakeSnapshot(path.rsplit("/", 1)[-1], data)
            for path, data in self._store.items()
            if path.startswith(self._prefix + "/") and path.count("/") == depth
        ]

    def stream(self):
        return iter(self._snapshots())

    def order_by(self, field: str) -> _FakeQuery:
        return _FakeQuery(self._snapshots(), field)


class FakeFirestoreClient:
    """Minimal stand-in for google.cloud.firestore.Client.

    Covers only the surface ``state/firestore.py`` uses. It exercises our serialisation
    and ordering logic; it says nothing about real Firestore's semantics, which is why
    that module carries an UNVERIFIED banner.
    """

    def __init__(self) -> None:
        self.data: dict = {}

    def collection(self, name: str) -> _FakeCollection:
        return _FakeCollection(self.data, name)


def avrae_message(
    *,
    title: str = "",
    description: str = "",
    fields=(),
    author: str = "",
    interaction_user_id=None,
    referenced_user_id=None,
    content: str = "",
):
    """Build a message dict shaped like discord.Message.to_dict()."""
    embed = {}
    if title:
        embed["title"] = title
    if description:
        embed["description"] = description
    if fields:
        embed["fields"] = [{"name": n, "value": v} for n, v in fields]
    if author:
        embed["author"] = {"name": author}

    msg = {"content": content, "embeds": [embed] if embed else []}
    if interaction_user_id is not None:
        msg["interaction"] = {"user": {"id": str(interaction_user_id)}}
    if referenced_user_id is not None:
        msg["referenced_message"] = {"author": {"id": str(referenced_user_id)}}
    return msg
