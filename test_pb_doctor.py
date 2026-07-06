"""Offline tests for pb_doctor's checks — each runs against a fake Phantombuster
client / in-memory DB, no live API. Run: python test_pb_doctor.py"""

import os

from sqlalchemy import create_engine

import pb_doctor as d
from model import db


class FakeClient:
    """Minimal stand-in: agents keyed by id, list + fetch."""
    def __init__(self, agents):
        self._agents = agents  # {id: {"name":..., "argument": ... }}

    def list_agents(self):
        return [{"id": k, **v} for k, v in self._agents.items()]

    def get_agent(self, agent_id):
        if agent_id not in self._agents:
            raise RuntimeError("Agent not found")
        return {"id": agent_id, **self._agents[agent_id]}


def _connected(cookie="li_at=xyz"):
    return {"name": "people", "argument": {"identities": [{"sessionCookie": cookie}]}}


def test_secrets_key_missing_and_present():
    os.environ.pop("APP_SECRETS_KEY", None)
    assert d.check_secrets_key().fatal
    # A real Fernet key makes it pass (secrets_box round-trips).
    import secrets_box
    os.environ["APP_SECRETS_KEY"] = secrets_box.generate_key()
    assert d.check_secrets_key().ok
    print("OK — secrets key: fatal when unset, ok with a real key")


def test_api_auth():
    assert d.check_api_auth(FakeClient({"1": {"name": "a"}})).ok

    class Broken:
        def list_agents(self): raise RuntimeError("401")
    assert d.check_api_auth(Broken()).fatal
    print("OK — api auth: ok on list, fatal on failure")


def test_agent_exists():
    pb = FakeClient({"AG": {"name": "CQC people"}})
    assert d.check_agent_exists(pb, "AG", label="search export agent", env="PB_X").ok
    assert d.check_agent_exists(pb, "NOPE", label="search export agent", env="PB_X").fatal
    assert d.check_agent_exists(pb, None, label="search export agent", env="PB_X").fatal
    print("OK — agent exists: ok when found, fatal when missing/unset")


def test_source_cookie():
    os.environ.pop("LINKEDIN_SESSION_COOKIE", None)
    pb = FakeClient({"SRC": _connected()})
    assert d.check_source_cookie(pb, "SRC").ok
    # An agent with no connected identity fails.
    pb_bare = FakeClient({"SRC": {"name": "x", "argument": {}}})
    assert d.check_source_cookie(pb_bare, "SRC").fatal
    # Unset with no env override fails.
    assert d.check_source_cookie(pb, None).fatal
    # The env override satisfies it without any agent.
    os.environ["LINKEDIN_SESSION_COOKIE"] = "li_at=env"
    assert d.check_source_cookie(pb, None).ok
    os.environ.pop("LINKEDIN_SESSION_COOKIE")
    print("OK — source cookie: needs a connected identity or the env override")


def test_source_cookie_tolerates_json_string_argument():
    import json
    pb = FakeClient({"SRC": {"name": "x",
                             "argument": json.dumps({"identities": [{"sessionCookie": "c"}]})}})
    assert d.check_source_cookie(pb, "SRC").ok
    print("OK — source cookie: parses the JSON-string argument variant")


def test_schema_check():
    engine = create_engine("sqlite:///:memory:")
    db.metadata.create_all(engine)  # fully current → no pending DDL
    assert d.check_schema(engine).ok
    print("OK — schema check: ok when the DB matches model.py")


def test_report_exit_codes():
    assert d.report([d.Check("a", True, "")]) == 0    # pass
    assert d.report([d.Check("a", False, "")]) == 1   # fatal
    print("OK — report: exit 0 on pass, 1 on fatal")


if __name__ == "__main__":
    test_secrets_key_missing_and_present()
    test_api_auth()
    test_agent_exists()
    test_source_cookie()
    test_source_cookie_tolerates_json_string_argument()
    test_schema_check()
    test_report_exit_codes()
    print("\nAll pb_doctor tests passed.")
