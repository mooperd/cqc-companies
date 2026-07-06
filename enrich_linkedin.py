"""Ingest LinkedIn profiles scraped by Phantombuster into Person + Role
(ADR 0016, WS3).

Identification phantoms return people Companies House cannot see (non-director
influencers: operations directors, registered managers, heads of care). Each
scraped profile find-or-creates a `Person` and attaches a low-confidence `Role`
with `source = phantombuster:<phantom>`.

Two ADR-0016 guarantees live here:
- **linkedin_url is the dedup key.** The same profile re-scraped is the same
  `Person` (exact URL match), stronger than name.
- **No auto-merge into Companies House directors.** LinkedIn carries no DOB, so
  correlation falls to ADR 0014's no-DOB path, which only matches among
  DOB-less people — a DOB-anchored CH director is never absorbed. The duplicate
  is tolerated and flagged `match_confidence='low'`; a provider-scoped name match
  becomes a merge-review Task once the Task system exists (Phase 4).

The runtime is `PhantomRun` (model.py): launch → poll → fetch → ingest, run under
a `User`'s LinkedIn session + Phantombuster key. The live driver
(`run_identification_phantom`) needs those credentials and so is the gated WS4
path; `sync_profiles` / `ingest_run` are exercised offline against fixtures.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import sys
import time

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import enrich_people as ep
import suppression
from enrich_people import Identity
from linkedin_profiles import ScrapedProfile, parse_profiles
from model import Person, Provider, Role, User, PhantomRun, db
from phantombuster import Phantombuster  # phantombuster-lib (the retired stdlib client's replacement)

logger = logging.getLogger(__name__)

# LinkedIn-sourced roles are self-reported and DOB-less → low confidence
# (ADR 0013 §3: LinkedIn is authoritative only for people CH cannot see).
CONFIDENCE = "low"
# Coarse role bucket for a scraped influencer; the scraped headline is kept in
# Role.control_nature (a finer taxonomy is deferred — ADR 0016 §3).
ROLE_TYPE = "influencer"

_DEFAULT_POLL = 10.0      # seconds between container status polls
_DEFAULT_TIMEOUT = 600.0  # give up on a run after this long


def role_source(phantom: str) -> str:
    return f"phantombuster:{phantom}"


def _identity_from_profile(profile: ScrapedProfile) -> Identity:
    """Parse a LinkedIn display name into the ADR 0014 identity. LinkedIn names are
    'Forename(s) Surname' — the PSC parser handles exactly that (and drops any
    leading title). No DOB/nationality from LinkedIn → a low-confidence identity."""
    surname, forenames = ep._split_psc_name(profile.name)
    return Identity(profile.name, surname, forenames, None, None, None)


def find_or_create_linkedin_person(session, identity: Identity,
                                   linkedin_url: str | None) -> tuple[Person | None, bool]:
    """Resolve a scraped profile to a Person. `linkedin_url` is the primary key
    (exact match = same person); otherwise fall back to ADR 0014's no-DOB name
    correlation, which by construction never merges into a DOB-anchored CH
    director. Backfills `linkedin_url` onto a name-matched person.

    Returns (None, False) if the contact is suppressed (ADR 0017 §5) — checked on
    the `linkedin_url` here and on the name inside `ep.find_or_create_person`."""
    if linkedin_url and suppression.is_suppressed(session, linkedin_url=linkedin_url):
        return None, False
    if linkedin_url:
        existing = session.query(Person).filter_by(linkedin_url=linkedin_url).first()
        if existing is not None:
            return existing, False
    # Enforce the no-merge-into-CH-director guarantee (ADR 0016 §5): LinkedIn
    # identities carry no DOB, so ep.find_or_create_person's no-DOB branch only
    # matches DOB-less people — a DOB-anchored CH director can never be absorbed.
    # Assert it rather than rely on the emergent property.
    assert identity.dob_year is None, "LinkedIn identities must be DOB-less (ADR 0016 §5)"
    person, created = ep.find_or_create_person(session, identity)
    if person is None:
        return None, False  # name-suppressed (ADR 0017 §5)
    if linkedin_url and not person.linkedin_url:
        person.linkedin_url = linkedin_url
    return person, created


def sync_profiles(session, provider: Provider, profiles, phantom: str) -> dict:
    """Correlate scraped profiles into Person rows and upsert their LinkedIn Roles
    for this provider. Idempotent on (person, provider, source). Caller commits."""
    source = role_source(phantom)
    existing = {
        r.person_id: r
        for r in session.query(Role).filter(
            Role.provider_id == provider.id, Role.source == source
        )
    }
    persons_created = roles_created = roles_updated = suppressed = 0
    for profile in profiles:
        identity = _identity_from_profile(profile)
        if not identity.surname:
            continue  # unparseable name — skip rather than create a junk Person
        person, created = find_or_create_linkedin_person(session, identity, profile.linkedin_url)
        if person is None:
            suppressed += 1  # erased contact — never re-create (ADR 0017 §5)
            continue
        persons_created += int(created)

        role = existing.get(person.id)
        if role is None:
            role = Role(person_id=person.id, provider_id=provider.id, source=source)
            session.add(role)
            existing[person.id] = role
            roles_created += 1
        elif role.control_nature == profile.headline and role.role_type == ROLE_TYPE:
            continue  # no change — keep idempotent re-syncs quiet
        else:
            roles_updated += 1
        role.role_type = ROLE_TYPE
        role.confidence = CONFIDENCE
        role.control_nature = profile.headline  # the scraped LinkedIn headline

    return {
        "persons_created": persons_created,
        "roles_created": roles_created,
        "roles_updated": roles_updated,
        "suppressed": suppressed,
        "profiles": len(profiles),
    }


def ingest_run(session, run: PhantomRun, profiles, credits_spent: int | None = None) -> dict:
    """Apply a finished run's scraped profiles to its target provider and close the
    run out. Caller commits."""
    provider = session.get(Provider, run.provider_id) if run.provider_id else None
    if provider is None:
        run.status, run.error = "failed", "no target provider for ingest"
        return {"persons_created": 0, "roles_created": 0, "roles_updated": 0, "profiles": 0}
    stats = sync_profiles(session, provider, profiles, run.phantom)
    run.status = "finished"
    run.finished_at = dt.datetime.now(dt.timezone.utc)
    if credits_spent is not None:
        run.credits_spent = credits_spent
    logger.info("ingested run %s (%s) for provider %s: %s",
                run.id, run.phantom, provider.id, stats)
    return stats


def run_identification_phantom(
    session, user: User, phantom: str, agent_id: str, argument: dict,
    provider: Provider | None = None, client: Phantombuster | None = None,
    poll: float = _DEFAULT_POLL, timeout: float = _DEFAULT_TIMEOUT,
    bonus: bool = False,
) -> PhantomRun:
    """Gated live driver (WS4): create a PhantomRun, launch the agent under the
    user's LinkedIn session + Phantombuster key, poll to completion, fetch the
    result rows, and ingest. Needs real per-user credentials — not run offline.
    Caller commits.

    Transport is phantombuster-lib's ``Phantombuster`` (PWS1). ``client`` is
    injectable for testing; by default one is built from the user's API key.

    ``bonus`` selects how ``argument`` is applied. A full ``argument`` *replaces*
    the agent's saved argument for the run; a store phantom (e.g. Search Export)
    keeps its connected LinkedIn identity + limits in that saved argument, so we
    pass only our per-run keys as ``bonus_argument`` (merged onto the saved base)
    instead of clobbering it. ``bonus=True`` does that; the recorded ``input`` is
    the same either way.
    """
    client = client or Phantombuster(user.phantombuster_api_key)
    run = PhantomRun(phantom=phantom, user_id=user.id,
                     provider_id=provider.id if provider else None,
                     input=argument, status="queued")
    session.add(run)
    session.flush()

    # The phantom runs under the LinkedIn session connected inside Phantombuster
    # (its browser extension writes the sessionCookie) — we don't hold or inject it
    # (ADR 0016 amendment 2026-07-05).
    if bonus:
        container_id = client.launch(agent_id, bonus_argument=argument)
    else:
        container_id = client.launch(agent_id, argument=argument)
    run.status, run.launched_at = "launched", dt.datetime.now(dt.timezone.utc)

    # Poll against a wall-clock deadline (not a += poll accumulator, which drifts
    # by each get_container's own latency). The lib's container status reaches a
    # terminal "finished" | "error"; success is a clean "finished" (lastEndStatus).
    deadline = time.monotonic() + timeout
    container: dict = {}
    while time.monotonic() < deadline:
        container = client.get_container(container_id)
        if container.get("status") in ("finished", "error"):
            break
        run.status = "running"
        time.sleep(poll)
    else:
        run.status, run.error = "failed", "timed out waiting for the phantom"
        return run

    if not (container.get("status") == "finished" and container.get("lastEndStatus") == "success"):
        run.status, run.error = "failed", f"phantom ended: {container.get('lastEndStatus') or container.get('status')}"
        return run

    profiles = parse_profiles(client.get_result(container_id))
    credits = container.get("creditUsed") or container.get("credits")
    ingest_run(session, run, profiles, credits_spent=credits)
    return run


# --- PWS3: acquire a provider's people via LinkedIn Search Export ---------------

# The identification phantom used for company-scoped people discovery (ADR 0016 §1).
# The store "LinkedIn Search Export" phantom; its per-run input key is
# `linkedInSearchUrl` (confirmed via agents/fetch, 2026-07-06 — not the `search`
# we'd inferred).
SEARCH_EXPORT_PHANTOM = "linkedin-search-export"
SEARCH_EXPORT_URL_KEY = "linkedInSearchUrl"
_LINKEDIN_PEOPLE_SEARCH = "https://www.linkedin.com/search/results/people/"


def company_people_search_url(company_id: str) -> str:
    """A LinkedIn people-search URL filtered to a company's current employees
    (ADR 0016 amendment: ``currentCompany=["<companyId>"]``) — the input the Search
    Export phantom scrapes. `company_id` is `Provider.linkedin_company_id` (PWS2)."""
    from urllib.parse import quote

    facet = quote(f'["{company_id}"]')
    return f"{_LINKEDIN_PEOPLE_SEARCH}?currentCompany={facet}"


def run_company_people(
    session, user: User, provider: Provider, agent_id: str, *,
    phantom: str = SEARCH_EXPORT_PHANTOM, client: Phantombuster | None = None,
    extra_argument: dict | None = None, poll: float = _DEFAULT_POLL,
    timeout: float = _DEFAULT_TIMEOUT,
) -> PhantomRun:
    """Gated live driver (PWS3): discover a provider's people via a LinkedIn Search
    Export filtered to its resolved company, ingesting them into `Person`/`Role`.

    Requires `provider.linkedin_company_id` (resolve it first — PWS2). Delegates to
    `run_identification_phantom` for the launch→poll→fetch→parse→ingest lifecycle,
    so every scraped person becomes a low-confidence `Person` + a
    `phantombuster:<phantom>` `Role`, never merged into a DOB-anchored CH director.
    """
    if not provider.linkedin_company_id:
        raise ValueError(
            f"provider {provider.id} has no linkedin_company_id — resolve it first (PWS2)"
        )
    # Override only the search URL as a bonus argument — the agent's saved base
    # argument (its connected LinkedIn identity/cookie, result limits, searchType)
    # is preserved. Sending a full `argument` would replace all of that.
    argument = {SEARCH_EXPORT_URL_KEY: company_people_search_url(provider.linkedin_company_id)}
    if extra_argument:
        argument.update(extra_argument)
    return run_identification_phantom(
        session, user, phantom, agent_id, argument, provider=provider,
        client=client, poll=poll, timeout=timeout, bonus=True,
    )


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="enrich_linkedin",
        description="Ingest Phantombuster LinkedIn profiles into Person + Role.",
    )
    p.add_argument("--user-id", type=int, required=True, help="User to run the phantom as")
    p.add_argument("--provider-id", type=int, required=True, help="target provider")
    p.add_argument("--phantom", required=True, help="phantom kind, e.g. company-people-scraper")
    p.add_argument("--agent-id", required=True, help="Phantombuster agent id")
    p.add_argument("--argument", default="{}", help="JSON string of the phantom's input args")
    return p


def main(argv: list[str] | None = None) -> int:
    import json

    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    engine = create_engine(
        os.getenv("DATABASE_URL", "postgresql://darwinist:darwinist@localhost:5432/darwinist")
    )
    db.metadata.create_all(engine)
    with Session(engine) as session:
        user = session.get(User, args.user_id)
        provider = session.get(Provider, args.provider_id)
        if user is None or provider is None:
            logger.error("user %s or provider %s not found", args.user_id, args.provider_id)
            return 1
        run = run_identification_phantom(
            session, user, args.phantom, args.agent_id, json.loads(args.argument),
            provider=provider,
        )
        session.commit()
        logger.info("run %s finished: status=%s", run.id, run.status)
    return 0


if __name__ == "__main__":
    sys.exit(main())
