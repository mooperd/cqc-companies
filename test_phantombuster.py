#!/usr/bin/env python3
"""Offline tests for the Phantombuster client (ADR 0016 WS2).

Exercises the pure parsing + the transport against fixtures with a mocked HTTP
layer — no live key. The live exit criterion (a real scrape) is gated on a
Phantombuster account + ADR 0017; field shapes here are representative and
flagged for live validation.

Run with: python test_phantombuster.py
"""

import json

import phantombuster as pb

# A representative Company People Scraper page: two real people (varying field
# names across the kinds of phantoms), one nameless/corporate row to drop.
_FIXTURE_ROWS = [
    {
        "fullName": "Jane Smith",
        "linkedInProfileUrl": "https://www.linkedin.com/in/jane-smith",
        "title": "Director of Care",
        "companyName": "Acme Care Ltd",
        "location": "Manchester, UK",
    },
    {
        "firstName": "Bob", "lastName": "Jones",
        "profileUrl": "https://www.linkedin.com/in/bob-jones",
        "headline": "Registered Manager",
        "company": "Acme Care Ltd",
    },
    {"companyName": "Acme Care Ltd"},  # no name → dropped
]


def test_parse_profiles_tolerant_fields():
    profiles = pb.parse_profiles(_FIXTURE_ROWS)
    assert len(profiles) == 2, "the nameless row must be dropped"
    jane, bob = profiles
    assert jane.name == "Jane Smith"
    assert jane.linkedin_url == "https://www.linkedin.com/in/jane-smith"
    assert jane.headline == "Director of Care"
    assert jane.company == "Acme Care Ltd"
    assert jane.location == "Manchester, UK"
    # Name assembled from first/last; alternate url/headline keys resolved.
    assert bob.name == "Bob Jones"
    assert bob.linkedin_url == "https://www.linkedin.com/in/bob-jones"
    assert bob.headline == "Registered Manager"
    assert bob.location is None
    print("OK — parse_profiles: tolerant field mapping, drops nameless rows")


def test_result_rows_unwraps_resultobject():
    # resultObject as a JSON-encoded string (the Phantombuster convention).
    assert pb._result_rows({"resultObject": json.dumps(_FIXTURE_ROWS)}) == _FIXTURE_ROWS
    # dict-wrapped rows under a data/results key.
    assert pb._result_rows({"resultObject": {"data": _FIXTURE_ROWS}}) == _FIXTURE_ROWS
    # absent / empty.
    assert pb._result_rows({}) == []
    assert pb._result_rows({"resultObject": None}) == []
    print("OK — _result_rows: unwraps JSON-string and dict-wrapped resultObject")


def test_container_status_helpers():
    assert pb.container_finished({"status": "finished"}) is True
    assert pb.container_finished({"status": "running"}) is False
    assert pb.container_succeeded({"status": "finished", "lastEndStatus": "success"}) is True
    assert pb.container_succeeded({"status": "finished", "lastEndStatus": "error"}) is False
    print("OK — container_finished / container_succeeded")


def test_launch_agent_extracts_container_id():
    calls = []

    def fake_request(method, path, key, body=None):
        calls.append((method, path, body))
        return {"status": "success", "data": {"containerId": "C123"}}

    original = pb._request
    pb._request = fake_request
    try:
        cid = pb.launch_agent("AGENT1", {"companyUrl": "x", "sessionCookie": "li"}, api_key="k")
        assert cid == "C123"
        method, path, body = calls[0]
        assert method == "POST" and path == "/api/v2/agents/launch"
        assert body == {"id": "AGENT1", "argument": {"companyUrl": "x", "sessionCookie": "li"}}
    finally:
        pb._request = original
    print("OK — launch_agent: POSTs the argument, extracts containerId from data envelope")


def test_fetch_result_parses_profiles():
    def fake_request(method, path, key, body=None):
        assert path == "/api/v2/agents/fetch-output?id=AGENT1"
        return {"data": {"resultObject": json.dumps(_FIXTURE_ROWS)}}

    original = pb._request
    pb._request = fake_request
    try:
        profiles = pb.fetch_result("AGENT1", api_key="k")
        assert [p.name for p in profiles] == ["Jane Smith", "Bob Jones"]
    finally:
        pb._request = original
    print("OK — fetch_result: fetch-output → parsed profiles")


def test_resolve_api_key():
    import os

    saved = os.environ.pop(pb.API_KEY_ENV, None)
    try:
        try:
            pb.resolve_api_key(None)
            assert False, "must raise when no key is set"
        except RuntimeError:
            pass
        assert pb.resolve_api_key("explicit") == "explicit"
        os.environ[pb.API_KEY_ENV] = "env-key"
        assert pb.resolve_api_key() == "env-key"
    finally:
        if saved is None:
            os.environ.pop(pb.API_KEY_ENV, None)
        else:
            os.environ[pb.API_KEY_ENV] = saved
    print("OK — resolve_api_key: explicit > env, raises when missing")


def test_request_retries_5xx():
    import io
    import urllib.error

    calls = []

    def fake_urlopen(req, timeout=60):
        calls.append(1)
        if len(calls) == 1:
            raise urllib.error.HTTPError(req.full_url, 503, "Service Unavailable", {}, None)
        return io.BytesIO(b'{"status":"success","data":{}}')

    orig_open, orig_sleep = pb.urllib.request.urlopen, pb.time.sleep
    pb.urllib.request.urlopen = fake_urlopen
    pb.time.sleep = lambda _s: None
    try:
        assert pb._request("GET", "/x", "key") == {"status": "success", "data": {}}
        assert len(calls) == 2, "should retry the 503 once then succeed"
    finally:
        pb.urllib.request.urlopen = orig_open
        pb.time.sleep = orig_sleep
    print("OK — _request retries transient 5xx then succeeds")


if __name__ == "__main__":
    test_parse_profiles_tolerant_fields()
    test_result_rows_unwraps_resultobject()
    test_container_status_helpers()
    test_launch_agent_extracts_container_id()
    test_fetch_result_parses_profiles()
    test_resolve_api_key()
    test_request_retries_5xx()
    print("\nAll Phantombuster offline tests passed.")
