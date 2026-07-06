"""Offline tests for run_extraction's testable helpers — capturing client, user
find-or-create, provider selection. No live API. Run: python test_run_extraction.py"""

import os

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import secrets_box
os.environ.setdefault("APP_SECRETS_KEY", secrets_box.generate_key())

import run_extraction as rx
from model import Provider, User, db


def _session() -> Session:
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(engine)
    return Session(engine)


def test_capturing_client_records_and_delegates():
    class Inner:
        def get_result(self, cid): return [{"name": "Jane", "profileUrl": "u"}]
        def launch(self, agent_id, **kw): return "C1"
        attr = "passthrough"

    c = rx._CapturingClient(Inner())
    assert c.get_result("C1") == [{"name": "Jane", "profileUrl": "u"}]
    assert c.last_result == [{"name": "Jane", "profileUrl": "u"}]  # captured
    assert c.launch("AG") == "C1"          # delegated method
    assert c.attr == "passthrough"          # delegated attribute
    print("OK — capturing client: records get_result, delegates the rest")


def test_find_or_create_user_is_idempotent():
    with _session() as s:
        u1 = rx.find_or_create_user(s, "rob@shape.build", "pb-key")
        assert u1.id and u1.phantombuster_api_key == "pb-key"
        u2 = rx.find_or_create_user(s, "rob@shape.build", "pb-key-2")
        assert u2.id == u1.id                         # same row
        assert u2.phantombuster_api_key == "pb-key-2"  # key refreshed
        assert s.query(User).count() == 1
    print("OK — find_or_create_user: idempotent on email, refreshes the key")


def test_pick_provider_by_id_and_name():
    with _session() as s:
        p = Provider(name="Barchester Healthcare Homes Limited", active=True)
        inactive = Provider(name="Barchester Old Ltd", active=False)
        s.add_all([p, inactive])
        s.flush()

        assert rx.pick_provider(s, provider_id=p.id, name=None).id == p.id
        # Name match returns the active one, case-insensitive substring.
        assert rx.pick_provider(s, provider_id=None, name="barchester").id == p.id
    print("OK — pick_provider: by id, and first active name match")


if __name__ == "__main__":
    test_capturing_client_records_and_delegates()
    test_find_or_create_user_is_idempotent()
    test_pick_provider_by_id_and_name()
    print("\nAll run_extraction tests passed.")
