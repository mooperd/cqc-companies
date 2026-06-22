"""Companies House API client — fetch a company's officers (directors).

WS1 of `docs/plans/companies-house-enrichment.md`, implementing the source
chosen in `docs/adr/0013-companies-house-source.md`. Given a Companies House
number (already stored on `Provider.companies_house_number`, carried through
from the CQC HSCA export), fetch its officers from the public API and return
them with role and appointment/resignation dates so a later pass (WS2) can map
director-class officers onto `Person` rows.

The API is free but key-gated: register a key at
<https://developer.company-information.service.gov.uk/> and supply it via the
`COMPANIES_HOUSE_API_KEY` environment variable. Auth is HTTP Basic with the key
as the username and an empty password.

Why stdlib-only: same reasoning as `cqc_refresh.py` — this module must import
cleanly in the PR-time smoke check without adding an HTTP dependency to
`requirements.txt`. urllib + json + base64 are sufficient.

Manual check once you have a key:

    COMPANIES_HOUSE_API_KEY=... python -m companies_house officers 02518546
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
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

# A Companies House key is tied to ONE environment (a "Test" application key
# works only against the sandbox, a "Live" key only against live), so we pick
# the key AND its matching base URL from a single COMPANIES_HOUSE_ENV switch —
# they can never be mismatched. Both keys can live in .env.local at once; flip
# the switch to choose. Default is live.
LIVE_API_BASE = "https://api.company-information.service.gov.uk"
SANDBOX_API_BASE = "https://api-sandbox.company-information.service.gov.uk"
ENV_VAR = "COMPANIES_HOUSE_ENV"  # "live" (default) | "test"
# Generic fallback key, used for whichever env is selected if the env-specific
# var below isn't set (handy for a minimal single-key setup).
API_KEY_ENV = "COMPANIES_HOUSE_API_KEY"
_BASES = {"live": LIVE_API_BASE, "test": SANDBOX_API_BASE}
_KEY_VARS = {"live": "COMPANIES_HOUSE_LIVE_KEY", "test": "COMPANIES_HOUSE_TEST_KEY"}
USER_AGENT = "cqc-companies-enrichment/1.0 (+https://github.com/mooperd/cqc-companies)"

# Officers come paginated; 50 keeps round-trips low without large payloads.
_PAGE_SIZE = 50
# Companies House allows ~600 requests / 5 min. We back off on 429 rather than
# rate-limit pre-emptively — the enrichment cadence is well under the ceiling.
_MAX_RETRIES = 4
_DEFAULT_BACKOFF = 5.0  # seconds, base for backoff when no Retry-After header
# Transient HTTP codes worth retrying (rate-limit + gateway/server errors).
_RETRY_CODES = {429, 500, 502, 503, 504}


# --- Data classes -------------------------------------------------------------


@dataclass(frozen=True)
class Officer:
    """A single Companies House officer appointment."""

    name: str
    role: str  # officer_role, e.g. "director", "secretary", "llp-member"
    appointed_on: dt.date | None
    resigned_on: dt.date | None
    # Partial DOB (month+year) and nationality — the correlation signals (ADR
    # 0014). Present for individuals; absent for corporate officers.
    dob_year: int | None = None
    dob_month: int | None = None
    nationality: str | None = None

    @property
    def is_active(self) -> bool:
        """True if the officer has not resigned (still appointed)."""
        return self.resigned_on is None


@dataclass(frozen=True)
class PSC:
    """A Companies House person with significant control."""

    name: str
    kind: str  # e.g. individual-person-with-significant-control, corporate-entity-...
    natures_of_control: tuple[str, ...]  # e.g. ownership-of-shares-75-to-100-percent
    notified_on: dt.date | None
    ceased_on: dt.date | None
    dob_year: int | None = None
    dob_month: int | None = None
    nationality: str | None = None

    @property
    def is_active(self) -> bool:
        """True if control has not ceased."""
        return self.ceased_on is None


@dataclass(frozen=True)
class FilingHistoryEntry:
    """One entry in a company's filing history — only the fields the freshness
    check needs: the filing `category` and its `date`."""

    category: str
    date: dt.date | None


class CompaniesHouseError(Exception):
    """A Companies House API request failed in a way the caller should handle.

    `status` carries the HTTP code when there was one, so callers can treat a
    404 specially (e.g. a company with no PSC filing).
    """

    def __init__(self, message: str, status: int | None = None):
        super().__init__(message)
        self.status = status


# --- Parsing (pure; no I/O, unit-testable without a key) -----------------------


def _parse_date(value: str | None) -> dt.date | None:
    """Parse a Companies House ISO date (YYYY-MM-DD); None/empty → None."""
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _parse_dob(item: dict) -> tuple[int | None, int | None]:
    """Extract (year, month) from a CH date_of_birth object (partial — no day)."""
    dob = item.get("date_of_birth") or {}
    return dob.get("year"), dob.get("month")


def _parse_officer(item: dict) -> Officer:
    year, month = _parse_dob(item)
    return Officer(
        name=item.get("name", "").strip(),
        role=item.get("officer_role", "").strip(),
        appointed_on=_parse_date(item.get("appointed_on")),
        resigned_on=_parse_date(item.get("resigned_on")),
        dob_year=year,
        dob_month=month,
        nationality=(item.get("nationality") or "").strip() or None,
    )


def _parse_psc(item: dict) -> PSC:
    year, month = _parse_dob(item)
    return PSC(
        name=item.get("name", "").strip(),
        kind=item.get("kind", "").strip(),
        natures_of_control=tuple(item.get("natures_of_control", []) or ()),
        notified_on=_parse_date(item.get("notified_on")),
        ceased_on=_parse_date(item.get("ceased_on")),
        dob_year=year,
        dob_month=month,
        nationality=(item.get("nationality") or "").strip() or None,
    )


def parse_officers_payload(payload: dict) -> list[Officer]:
    """Map one /officers JSON page's `items` array to `Officer`s."""
    return [_parse_officer(item) for item in payload.get("items", [])]


def parse_psc_payload(payload: dict) -> list[PSC]:
    """Map one /persons-with-significant-control page's `items` to `PSC`s."""
    return [_parse_psc(item) for item in payload.get("items", [])]


# Filing-history categories that signal an officer or PSC change — the only ones
# the freshness check (ADR 0015 WS4) cares about. Accounts, confirmation
# statements, etc. are ignored so the watermark doesn't over-trigger.
OFFICER_PSC_CATEGORIES = frozenset({"officers", "persons-with-significant-control"})


def _parse_filing(item: dict) -> FilingHistoryEntry:
    return FilingHistoryEntry(
        category=(item.get("category") or "").strip(),
        date=_parse_date(item.get("date")),
    )


def parse_filing_history_payload(payload: dict) -> list[FilingHistoryEntry]:
    """Map one /filing-history page's `items` array to `FilingHistoryEntry`s."""
    return [_parse_filing(item) for item in payload.get("items", [])]


def latest_relevant_filing_date(entries: list[FilingHistoryEntry]) -> dt.date | None:
    """The most recent filing date among officer/PSC-category entries — the
    watermark used to decide whether a company needs re-polling (ADR 0015 WS4).
    None when the company has filed nothing in those categories."""
    dates = [e.date for e in entries
             if e.category in OFFICER_PSC_CATEGORIES and e.date is not None]
    return max(dates) if dates else None


# --- HTTP ---------------------------------------------------------------------


def resolve_env() -> str:
    """The selected environment: "live" (default) or "test". Read at call time."""
    raw = os.getenv(ENV_VAR, "live").strip().lower()
    if raw in ("test", "sandbox"):
        return "test"
    if raw == "live":
        return "live"
    raise RuntimeError(f"{ENV_VAR} must be 'live' or 'test' (got {raw!r}).")


def resolve_api_key(api_key: str | None = None) -> str:
    """Return the explicit key, else the env-specific key (with a generic
    fallback). Raise if none is set."""
    if api_key:
        return api_key
    env = resolve_env()
    key = os.getenv(_KEY_VARS[env]) or os.getenv(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"Companies House API key missing for env '{env}': set "
            f"{_KEY_VARS[env]} (or {API_KEY_ENV}). Free key from "
            "https://developer.company-information.service.gov.uk/."
        )
    return key


def _api_base() -> str:
    # Derived from the selected env so key and base can't mismatch. Read at
    # call time so the CLI's dotenv load is already in effect.
    return _BASES[resolve_env()]


def _auth_header(api_key: str) -> str:
    # Basic auth: key as username, empty password → base64("key:").
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def _get_json(path: str, api_key: str) -> dict:
    """GET an API path (e.g. "/company/02518546/officers"), return parsed JSON.

    Retries on 429 honouring Retry-After. Raises CompaniesHouseError on 404
    (unknown company) and RuntimeError on 401 (bad key).
    """
    url = f"{_api_base()}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", _auth_header(api_key))
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            # Transient: 429 rate-limit and 5xx gateway errors. Over tens of
            # thousands of requests these happen; back off and retry rather than
            # aborting the run.
            if err.code in _RETRY_CODES and attempt < _MAX_RETRIES:
                retry_after = err.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else _DEFAULT_BACKOFF * attempt
                logger.warning("HTTP %d from Companies House; retry %d in %.0fs",
                               err.code, attempt, delay)
                time.sleep(delay)
                continue
            if err.code == 401:
                env = resolve_env()
                raise RuntimeError(
                    f"Companies House rejected the API key (401) for env "
                    f"'{env}'. A key is tied to one environment — confirm "
                    f"{_KEY_VARS[env]} (or {API_KEY_ENV}) is a '{env}' "
                    "application key, not the other environment's. (A 'Test' "
                    "key works only against the sandbox; a 'Live' key only "
                    "against live.)"
                ) from err
            if err.code == 404:
                raise CompaniesHouseError(f"not found: {path}", status=404) from err
            raise CompaniesHouseError(f"HTTP {err.code} for {path}", status=err.code) from err
        except urllib.error.URLError as err:
            # Connection reset / DNS / timeout — also transient over a long run.
            if attempt < _MAX_RETRIES:
                logger.warning("network error from Companies House; retry %d: %s",
                               attempt, err)
                time.sleep(_DEFAULT_BACKOFF * attempt)
                continue
            raise CompaniesHouseError(f"network error for {path}: {err}") from err
    raise CompaniesHouseError(f"giving up after {_MAX_RETRIES} attempts: {path}")


# --- Public API ---------------------------------------------------------------


def _fetch_all_pages(resource_path: str, parse_fn, key: str) -> list:
    """Follow Companies House pagination for a list resource, returning every
    item. Terminates on a short page (fewer than _PAGE_SIZE items) — relies only
    on page size, so at most one spare request when the count is an exact
    multiple of _PAGE_SIZE, which is rare and cheap.
    """
    items: list = []
    start_index = 0
    while True:
        path = f"{resource_path}?items_per_page={_PAGE_SIZE}&start_index={start_index}"
        page = parse_fn(_get_json(path, key))
        items.extend(page)
        if len(page) < _PAGE_SIZE:
            break
        start_index += _PAGE_SIZE
    return items


def fetch_officers(
    company_number: str,
    api_key: str | None = None,
    active_only: bool = False,
) -> list[Officer]:
    """Fetch all officers for a company, following pagination.

    `active_only=True` filters out resigned officers. Role filtering (directors
    vs secretaries) is left to the caller — this returns every officer the API
    reports so the appointment/resignation distinction stays visible.
    """
    key = resolve_api_key(api_key)
    path = f"/company/{company_number.strip()}/officers"
    officers = _fetch_all_pages(path, parse_officers_payload, key)
    if active_only:
        officers = [o for o in officers if o.is_active]
    return officers


def fetch_psc(
    company_number: str,
    api_key: str | None = None,
    active_only: bool = False,
) -> list[PSC]:
    """Fetch all persons with significant control for a company, paginated.

    `active_only=True` filters out ceased control. Kind filtering (individuals vs
    corporate entities) is left to the caller, mirroring fetch_officers.
    """
    key = resolve_api_key(api_key)
    path = f"/company/{company_number.strip()}/persons-with-significant-control"
    try:
        pscs = _fetch_all_pages(path, parse_psc_payload, key)
    except CompaniesHouseError as err:
        # A PSC 404 means "no PSC filing", which is common and innocuous — an
        # unknown company number would already have 404'd on the officers call
        # the caller makes first, so reaching here means the company exists.
        if err.status == 404:
            return []
        raise
    if active_only:
        pscs = [p for p in pscs if p.is_active]
    return pscs


def fetch_filing_history(
    company_number: str,
    api_key: str | None = None,
) -> list[FilingHistoryEntry]:
    """Fetch a company's filing history, following pagination.

    This is the cheap gate WS4 calls before the heavier officers/PSC re-poll: a
    404 (unknown or dissolved company) propagates as `CompaniesHouseError` so the
    caller can skip that provider, mirroring how `fetch_officers` surfaces 404.

    All categories are fetched and filtered locally (`latest_relevant_filing_date`)
    rather than asking the API for a single category — most care companies have
    well under one page of filings, so this stays a one-or-two request check;
    `latest_relevant_filing_date` keeps only the officer/PSC ones.
    """
    key = resolve_api_key(api_key)
    path = f"/company/{company_number.strip()}/filing-history"
    return _fetch_all_pages(path, parse_filing_history_payload, key)


# --- CLI ----------------------------------------------------------------------


def _officer_to_dict(officer: Officer) -> dict:
    return {
        "name": officer.name,
        "role": officer.role,
        "appointed_on": officer.appointed_on.isoformat() if officer.appointed_on else None,
        "resigned_on": officer.resigned_on.isoformat() if officer.resigned_on else None,
        "is_active": officer.is_active,
    }


def _psc_to_dict(psc: PSC) -> dict:
    return {
        "name": psc.name,
        "kind": psc.kind,
        "natures_of_control": list(psc.natures_of_control),
        "notified_on": psc.notified_on.isoformat() if psc.notified_on else None,
        "ceased_on": psc.ceased_on.isoformat() if psc.ceased_on else None,
        "is_active": psc.is_active,
    }


def _cmd_officers(args: argparse.Namespace) -> int:
    logger.info("env=%s (%s)", resolve_env(), _api_base())
    officers = fetch_officers(args.company_number, active_only=args.active_only)
    print(json.dumps([_officer_to_dict(o) for o in officers], indent=2))
    logger.info(
        "%d officers (%d active)",
        len(officers),
        sum(1 for o in officers if o.is_active),
    )
    return 0


def _cmd_psc(args: argparse.Namespace) -> int:
    logger.info("env=%s (%s)", resolve_env(), _api_base())
    pscs = fetch_psc(args.company_number, active_only=args.active_only)
    print(json.dumps([_psc_to_dict(p) for p in pscs], indent=2))
    logger.info("%d PSCs (%d active)", len(pscs), sum(1 for p in pscs if p.is_active))
    return 0


def _cmd_filing(args: argparse.Namespace) -> int:
    logger.info("env=%s (%s)", resolve_env(), _api_base())
    entries = fetch_filing_history(args.company_number)
    latest = latest_relevant_filing_date(entries)
    print(json.dumps(
        [{"category": e.category, "date": e.date.isoformat() if e.date else None}
         for e in entries], indent=2))
    logger.info("%d filings; latest officer/PSC filing: %s",
                len(entries), latest.isoformat() if latest else "none")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="companies_house",
        description="Fetch a company's officers and PSCs from the Companies House API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    officers = sub.add_parser("officers", help="fetch officers for a company number")
    officers.add_argument("company_number", help="Companies House number, e.g. 02518546")
    officers.add_argument(
        "--active-only", action="store_true", help="exclude resigned officers"
    )

    psc = sub.add_parser("psc", help="fetch persons with significant control")
    psc.add_argument("company_number", help="Companies House number, e.g. 02518546")
    psc.add_argument("--active-only", action="store_true", help="exclude ceased PSCs")

    filing = sub.add_parser("filing", help="fetch filing history + latest officer/PSC date")
    filing.add_argument("company_number", help="Companies House number, e.g. 02518546")

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Load local env so the CLI picks up COMPANIES_HOUSE_API_KEY without the
    # caller exporting it. .env.local (real secrets) overrides .env (dev
    # defaults). Lazy import keeps the module itself stdlib-only, so the CI
    # smoke check can `import companies_house` with no dependency installed.
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    handlers = {"officers": _cmd_officers, "psc": _cmd_psc, "filing": _cmd_filing}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
