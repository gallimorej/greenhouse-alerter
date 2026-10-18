"""Store backends for tests.

Every test using ``store_factory`` runs against MemoryStore, and also against FirestoreStore when
FIRESTORE_EMULATOR_HOST points at a running emulator (CI starts one).
"""

import os
import uuid

import pytest
import requests

from greenhouse.store import FirestoreStore, MemoryStore

EMULATOR = os.environ.get("FIRESTORE_EMULATOR_HOST")
EMULATOR_PROJECT = "demo-greenhouse"


def _firestore_factory():
    from google.cloud import firestore

    database = f"test-{uuid.uuid4().hex[:8]}"
    client = firestore.Client(project=EMULATOR_PROJECT, database=database)

    def make(docs=None):
        for path, data in (docs or {}).items():
            client.document(path).set(data)
        return FirestoreStore(client)

    def cleanup():
        requests.delete(
            f"http://{EMULATOR}/emulator/v1/projects/{EMULATOR_PROJECT}/databases/{database}/documents",
            timeout=5,
        )

    return make, cleanup


@pytest.fixture(params=["memory", "firestore"])
def store_factory(request):
    if request.param == "memory":
        yield MemoryStore
        return
    if not EMULATOR:
        pytest.skip("FIRESTORE_EMULATOR_HOST not set")
    make, cleanup = _firestore_factory()
    yield make
    cleanup()
