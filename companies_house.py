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

API_BASE = "https://api.company-information.service.gov.uk"
API_KEY_ENV = "COMPANIES_HOUSE_API_KEY"
USER_AGENT = "cqc-companies-enrichment/1.0 (+https://github.com/mooperd/cqc-companies)"

# Officers come paginated; 50 keeps round-trips low without large payloads.
_PAGE_SIZE = 50
# Companies House allows ~600 requests / 5 min. We back off on 429 rather than
# rate-limit pre-emptively — the enrichment cadence is well under the ceiling.
_MAX_RETRIES = 4
_DEFAULT_BACKOFF = 5.0  # seconds, used when a 429 carries no Retry-After


# --- Data classes -------------------------------------------------------------


@dataclass(frozen=True)
class Officer:
    """A single Companies House officer appointment."""

    name: str
    role: str  # officer_role, e.g. "director", "secretary", "llp-member"
    appointed_on: dt.date | None
    resigned_on: dt.date | None

    @property
    def is_active(self) -> bool:
        """True if the officer has not resigned (still appointed)."""
        return self.resigned_on is None


class CompaniesHouseError(Exception):
    """A Companies House API request failed in a way the caller should handle."""


# --- Parsing (pure; no I/O, unit-testable without a key) -----------------------


def _parse_date(value: str | None) -> dt.date | None:
    """Parse a Companies House ISO date (YYYY-MM-DD); None/empty → None."""
    if not value:
        return None
    return dt.date.fromisoformat(value)


def _parse_officer(item: dict) -> Officer:
    return Officer(
        name=item.get("name", "").strip(),
        role=item.get("officer_role", "").strip(),
        appointed_on=_parse_date(item.get("appointed_on")),
        resigned_on=_parse_date(item.get("resigned_on")),
    )


def parse_officers_payload(payload: dict) -> list[Officer]:
    """Map one /officers JSON page's `items` array to `Officer`s."""
    return [_parse_officer(item) for item in payload.get("items", [])]


# --- HTTP ---------------------------------------------------------------------


def resolve_api_key(api_key: str | None = None) -> str:
    """Return the explicit key, else the env key; raise if neither is set."""
    key = api_key or os.getenv(API_KEY_ENV)
    if not key:
        raise RuntimeError(
            f"Companies House API key missing: set {API_KEY_ENV} (free key from "
            "https://developer.company-information.service.gov.uk/)."
        )
    return key


def _auth_header(api_key: str) -> str:
    # Basic auth: key as username, empty password → base64("key:").
    token = base64.b64encode(f"{api_key}:".encode()).decode("ascii")
    return f"Basic {token}"


def _get_json(path: str, api_key: str) -> dict:
    """GET an API path (e.g. "/company/02518546/officers"), return parsed JSON.

    Retries on 429 honouring Retry-After. Raises CompaniesHouseError on 404
    (unknown company) and RuntimeError on 401 (bad key).
    """
    url = f"{API_BASE}{path}"
    req = urllib.request.Request(url)
    req.add_header("Authorization", _auth_header(api_key))
    req.add_header("User-Agent", USER_AGENT)
    req.add_header("Accept", "application/json")

    for attempt in range(1, _MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as err:
            if err.code == 429 and attempt < _MAX_RETRIES:
                retry_after = err.headers.get("Retry-After")
                delay = float(retry_after) if retry_after else _DEFAULT_BACKOFF
                logger.warning("429 from Companies House; retrying in %.0fs", delay)
                time.sleep(delay)
                continue
            if err.code == 401:
                raise RuntimeError(
                    "Companies House rejected the API key (401). Check "
                    f"{API_KEY_ENV}."
                ) from err
            if err.code == 404:
                raise CompaniesHouseError(f"not found: {path}") from err
            raise CompaniesHouseError(f"HTTP {err.code} for {path}") from err
    raise CompaniesHouseError(f"giving up after {_MAX_RETRIES} attempts: {path}")


# --- Public API ---------------------------------------------------------------


def fetch_officers(
    company_number: str,
    api_key: str | None = None,
    active_only: bool = False,
) -> list[Officer]:
    """Fetch all officers for a company, following pagination.

    `active_only=True` filters out resigned officers. Role filtering (directors
    vs secretaries) is left to the caller (WS2) — this returns every officer the
    API reports so the appointment/resignation distinction stays visible.
    """
    key = resolve_api_key(api_key)
    company_number = company_number.strip()
    officers: list[Officer] = []
    start_index = 0

    while True:
        path = (
            f"/company/{company_number}/officers"
            f"?items_per_page={_PAGE_SIZE}&start_index={start_index}"
        )
        page = parse_officers_payload(_get_json(path, key))
        officers.extend(page)
        # A short page (fewer than a full page of items) is the last one. This
        # relies only on page size, not the payload's `total_results` — one
        # spare request when the count is an exact multiple of _PAGE_SIZE,
        # which is rare and cheap at this volume.
        if len(page) < _PAGE_SIZE:
            break
        start_index += _PAGE_SIZE

    if active_only:
        officers = [o for o in officers if o.is_active]
    return officers


# --- CLI ----------------------------------------------------------------------


def _officer_to_dict(officer: Officer) -> dict:
    return {
        "name": officer.name,
        "role": officer.role,
        "appointed_on": officer.appointed_on.isoformat() if officer.appointed_on else None,
        "resigned_on": officer.resigned_on.isoformat() if officer.resigned_on else None,
        "is_active": officer.is_active,
    }


def _cmd_officers(args: argparse.Namespace) -> int:
    officers = fetch_officers(args.company_number, active_only=args.active_only)
    print(json.dumps([_officer_to_dict(o) for o in officers], indent=2))
    logger.info(
        "%d officers (%d active)",
        len(officers),
        sum(1 for o in officers if o.is_active),
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="companies_house",
        description="Fetch a company's officers from the Companies House API.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    officers = sub.add_parser("officers", help="fetch officers for a company number")
    officers.add_argument("company_number", help="Companies House number, e.g. 02518546")
    officers.add_argument(
        "--active-only", action="store_true", help="exclude resigned officers"
    )

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    handlers = {"officers": _cmd_officers}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
