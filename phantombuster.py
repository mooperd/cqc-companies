"""Phantombuster API client — launch a LinkedIn identification phantom, poll it,
and fetch its scraped profiles (ADR 0016, WS2).

Identification phantoms (read-only scrapes) feed `Person`/`Role` via
enrich_linkedin: Company People Scraper, Sales Navigator Search Export, Profile
Data. This module is the *transport*; the runtime model (`PhantomRun`) and the
profile→Person/Role mapping live in `model.py` / `enrich_linkedin.py`.

Auth is the `X-Phantombuster-Key-1` header with the account API key
(`PHANTOMBUSTER_API_KEY`, or a per-user key from `User`). Phantombuster runs are
asynchronous: `launch_agent` returns a container id, you poll `fetch_container`
until it is finished, then `fetch_result` returns the agent's scraped rows.

Why stdlib-only: same reasoning as `companies_house.py` — the module must import
cleanly in the PR-time smoke check without adding an HTTP dependency. urllib +
json are sufficient.

> Field shapes below (the profile result keys, the response envelopes) are
> representative of the v2 API and the common LinkedIn phantoms, and are flagged
> for validation against a live run — exactly the caveat ADR 0016 carries. The
> parsers are deliberately tolerant of key-name variation across phantoms.

Manual check once you have a key:

    PHANTOMBUSTER_API_KEY=... python -m phantombuster result <agentId>
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# --- Constants ----------------------------------------------------------------

API_BASE = "https://api.phantombuster.com"
API_KEY_ENV = "PHANTOMBUSTER_API_KEY"
AUTH_HEADER = "X-Phantombuster-Key-1"
USER_AGENT = "cqc-companies-enrichment/1.0 (+https://github.com/mooperd/cqc-companies)"

_MAX_RETRIES = 4
_DEFAULT_BACKOFF = 5.0  # seconds, base for backoff when no Retry-After header
_RETRY_CODES = {429, 500, 502, 503, 504}

# Identification (scrape) phantoms in scope (ADR 0016 §1). Action phantoms
# (message senders, auto-connect) are deliberately absent — they are outreach.
IDENTIFICATION_PHANTOMS = frozenset({
    "company-people-scraper",
    "sales-navigator-search-export",
    "profile-data",
})


# --- Data classes -------------------------------------------------------------


@dataclass(frozen=True)
class ScrapedProfile:
    """One LinkedIn profile a phantom returned — only the fields the CRM needs."""

    name: str
    linkedin_url: str | None
    headline: str | None
    company: str | None
    location: str | None


class PhantombusterError(Exception):
    """A Phantombuster API request failed in a way the caller should handle.

    `status` carries the HTTP code when there was one.
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- Parsing (pure; no I/O, unit-testable without a key) -----------------------


def _first(item: dict, *keys: str) -> str | None:
    """First non-empty value among `keys` (lowercase; caller lowercases the row)."""
    for key in keys:
        value = item.get(key)
        if value:
            return str(value).strip()
    return None


def parse_profile(item: dict) -> ScrapedProfile:
    """Map one phantom result row to a ScrapedProfile, tolerant of key variation.
    Keys are matched case-insensitively — Phantombuster is inconsistent about
    `linkedIn` vs `linkedin` casing across phantoms (spike: phantombuster-api)."""
    item = {str(k).lower(): v for k, v in item.items()}
    name = _first(item, "fullname", "name")
    if not name:
        first = _first(item, "firstname")
        last = _first(item, "lastname")
        name = " ".join(p for p in (first, last) if p) or ""
    return ScrapedProfile(
        name=name,
        linkedin_url=_first(
            item, "linkedinprofileurl", "profileurl", "linkedinurl", "profilelink",
        ),
        headline=_first(item, "headline", "title", "jobtitle", "occupation"),
        company=_first(item, "companyname", "company", "currentcompany"),
        location=_first(item, "location", "locationname"),
    )


def parse_profiles(rows: list[dict]) -> list[ScrapedProfile]:
    """Map a phantom's result rows to ScrapedProfiles, dropping rows with no name
    (corporate/blank entries — `Person` is contactable humans only, ADR 0014 §5)."""
    profiles = [parse_profile(row) for row in rows]
    return [p for p in profiles if p.name]


# v2 container status enum (agents/fetch-output prevStatusString):
# starting | running | finished | unknown | launch error. "finished" is the
# clean terminal; "launch error" is terminal-but-failed (gate on lastEndStatus
# below, never on status alone). "unknown" is treated as still-running.
_TERMINAL_STATES = frozenset({"finished", "launch error"})


def container_finished(payload: dict) -> bool:
    """True once a container has reached a terminal state (stop polling)."""
    return (payload.get("status") or "").lower() in _TERMINAL_STATES


def container_succeeded(payload: dict) -> bool:
    """True if a finished container ended successfully. `status == finished` does
    NOT imply success — a phantom can finish with lastEndStatus 'error' (e.g. an
    expired LinkedIn session cookie), so always gate on this."""
    return (payload.get("lastEndStatus") or "").lower() == "success"


def _result_rows(payload: dict) -> list[dict]:
    """Pull the scraped rows out of an agents/fetch-output response. The rows live
    in `resultObject` as a JSON-encoded string (Phantombuster's convention)."""
    result = payload.get("resultObject")
    if result is None:
        return []
    if isinstance(result, str):
        try:
            result = json.loads(result)
        except json.JSONDecodeError as err:
            raise PhantombusterError(f"resultObject is not valid JSON: {err}") from err
    if isinstance(result, dict):  # some phantoms wrap rows under a key
        result = result.get("data") or result.get("results") or []
    return result if isinstance(result, list) else []


# --- HTTP ---------------------------------------------------------------------


def resolve_api_key(api_key: str | None = None) -> str:
    """The explicit key, else `PHANTOMBUSTER_API_KEY`. Raises if neither is set."""
    key = api_key or os.getenv(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"No Phantombuster API key. Set {API_KEY_ENV} or pass one explicitly "
            "(per-user keys live on User.get_phantombuster_api_key())."
        )
    return key


def _request(method: str, path: str, api_key: str, body: dict | None = None) -> dict:
    """Make an API request, return parsed JSON. Retries transient 429/5xx."""
    url = f"{API_BASE}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header(AUTH_HEADER, api_key)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code in _RETRY_CODES and attempt < _MAX_RETRIES:
                retry_after = err.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else _DEFAULT_BACKOFF * attempt
                logger.warning("HTTP %d from Phantombuster; retry %d in %.0fs",
                               err.code, attempt, delay)
                time.sleep(delay)
                continue
            if err.code in (401, 403):
                raise RuntimeError(
                    f"Phantombuster rejected the API key ({err.code}). Confirm "
                    f"{API_KEY_ENV} (or the per-user key) is valid."
                ) from err
            raise PhantombusterError(f"HTTP {err.code} for {path}", status=err.code) from err
        except urllib.error.URLError as err:
            if attempt < _MAX_RETRIES:
                logger.warning("network error from Phantombuster; retry %d: %s", attempt, err)
                time.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            raise PhantombusterError(f"network error for {path}: {err}") from err
    raise PhantombusterError(f"giving up after {_MAX_RETRIES} attempts: {path}")


def _data(payload: dict) -> dict:
    """Phantombuster v2 wraps results as {"status":"success","data":{...}}; some
    endpoints return the object directly. Unwrap either."""
    inner = payload.get("data")
    return inner if isinstance(inner, dict) else payload


# --- Public API ---------------------------------------------------------------


def launch_agent(agent_id: str, argument: dict | None = None,
                 api_key: str | None = None, bonus_argument: dict | None = None) -> str:
    """Launch a phantom (agent), returning its container id.

    - `argument`: the full input. **Omit it (None) to use the phantom's SAVED
      UI config** — sending an argument *replaces* the saved config, so passing
      `{}` would wipe it (spike: phantombuster-api).
    - `bonus_argument`: merged into the saved config for THIS launch only — the
      right way to override one input (e.g. `spreadsheetUrl`) without clobbering
      the rest.

    Both are JSON-encoded to strings: v2 accepts object-or-string but v1 requires
    a string, so a string is the compatible form.
    """
    key = resolve_api_key(api_key)
    body: dict = {"id": agent_id}
    if argument is not None:
        body["argument"] = json.dumps(argument)
    if bonus_argument is not None:
        body["bonusArgument"] = json.dumps(bonus_argument)
    payload = _request("POST", "/api/v2/agents/launch", key, body=body)
    container_id = _data(payload).get("containerId")  # _data unwraps the JSend envelope
    if not container_id:
        raise PhantombusterError(f"launch returned no containerId: {payload}")
    return str(container_id)


def fetch_container(container_id: str, api_key: str | None = None) -> dict:
    """Fetch a container's status record (poll this until `container_finished`)."""
    key = resolve_api_key(api_key)
    return _data(_request("GET", f"/api/v2/containers/fetch?id={container_id}", key))


def fetch_agent(agent_id: str, api_key: str | None = None) -> dict:
    """Fetch an agent's record — including the S3 folder pointers its results live
    under (`orgS3Folder`, `s3Folder`)."""
    key = resolve_api_key(api_key)
    return _data(_request("GET", f"/api/v2/agents/fetch?id={agent_id}", key))


def result_json_url(agent_record: dict, filename: str = "result.json") -> str:
    """Build the public S3 URL of an agent's structured result file. The full
    `result.json` is the canonical row set — the `result.csv` is a lossy subset
    (only the first job/school, fewer columns), so always read the JSON
    (spike: phantombuster-api)."""
    org = agent_record.get("orgS3Folder")
    folder = agent_record.get("s3Folder")
    if not org or not folder:
        raise PhantombusterError(f"agent record missing S3 folders: {agent_record!r}")
    return f"https://phantombuster.s3.amazonaws.com/{org}/{folder}/{filename}"


def _get_url_json(url: str):
    """GET an absolute URL (the public S3 result file — no auth header) → JSON.
    Retries transient errors AND 403/404, which S3 can return briefly right after
    a run finishes (eventual consistency)."""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")
    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if (err.code in _RETRY_CODES or err.code in (403, 404)) and attempt < _MAX_RETRIES:
                time.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            raise PhantombusterError(f"HTTP {err.code} for result file {url}", status=err.code) from err
        except urllib.error.URLError as err:
            if attempt < _MAX_RETRIES:
                time.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            raise PhantombusterError(f"network error for {url}: {err}") from err
    raise PhantombusterError(f"giving up fetching {url}")


def fetch_result(agent_id: str, api_key: str | None = None,
                 filename: str = "result.json") -> list[ScrapedProfile]:
    """Fetch + parse an agent's most recent scraped profiles from its S3
    `result.json` (the canonical full row set). Keyed on the agent id, not its
    display name — phantoms get renamed (spike: phantombuster-api)."""
    key = resolve_api_key(api_key)
    rows = _get_url_json(result_json_url(fetch_agent(agent_id, key), filename))
    if isinstance(rows, dict):  # some phantoms wrap the array under a key
        rows = rows.get("data") or rows.get("results") or []
    return parse_profiles(rows if isinstance(rows, list) else [])


def fetch_result_object(agent_id: str, api_key: str | None = None) -> list[ScrapedProfile]:
    """Secondary path: parse the in-API `resultObject` (a JSON-encoded string set
    via the phantom's setResultObject). Smaller and not always populated — prefer
    `fetch_result` (S3) for the full rows (spike: phantombuster-api)."""
    key = resolve_api_key(api_key)
    payload = _data(_request("GET", f"/api/v2/agents/fetch-output?id={agent_id}", key))
    return parse_profiles(_result_rows(payload))


# --- CLI ----------------------------------------------------------------------


def _cmd_launch(args: argparse.Namespace) -> int:
    argument = json.loads(args.argument) if args.argument else {}
    container_id = launch_agent(args.agent_id, argument)
    print(json.dumps({"containerId": container_id}, indent=2))
    return 0


def _cmd_status(args: argparse.Namespace) -> int:
    payload = fetch_container(args.container_id)
    print(json.dumps(payload, indent=2))
    logger.info("finished=%s succeeded=%s", container_finished(payload), container_succeeded(payload))
    return 0


def _cmd_result(args: argparse.Namespace) -> int:
    profiles = fetch_result(args.agent_id)
    print(json.dumps([p.__dict__ for p in profiles], indent=2))
    logger.info("%d profiles", len(profiles))
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="phantombuster",
        description="Launch / poll / fetch a Phantombuster LinkedIn phantom.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    launch = sub.add_parser("launch", help="launch an agent, print its container id")
    launch.add_argument("agent_id", help="Phantombuster agent (phantom) id")
    launch.add_argument("--argument", help="JSON string of the phantom's input args")

    status = sub.add_parser("status", help="fetch a container's status")
    status.add_argument("container_id", help="container id from launch")

    result = sub.add_parser("result", help="fetch + parse an agent's scraped profiles")
    result.add_argument("agent_id", help="Phantombuster agent (phantom) id")

    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    handlers = {"launch": _cmd_launch, "status": _cmd_status, "result": _cmd_result}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
