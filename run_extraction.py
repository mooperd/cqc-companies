"""One-provider live LinkedIn extraction — the gated end-to-end driver (ADR 0016).

Drives the real pipeline for a single provider so we can gather evidence before
committing to it at scale: resolve the provider's brand to a LinkedIn company id
(PWS2), then scrape that company's people via the Search Export (PWS3) into
low-confidence Person/Role rows. Live — it spends Phantombuster credits and
touches real personal data (ADR 0017), so it is staged and each live step is
explicit:

    # 1. Dry preview — pick the provider, show what WOULD run, launch nothing:
    uv run python run_extraction.py --provider-name "Barchester"

    # 2. Resolve only — one live resolver run → store linkedin_company_id:
    uv run python run_extraction.py --provider-name "Barchester" --resolve

    # 3. Full — resolve (or reuse a stored id) then scrape the people:
    uv run python run_extraction.py --provider-name "Barchester" --resolve --scrape

    # Skip the resolver with a company id you already have (e.g. from LinkedIn):
    uv run python run_extraction.py --provider-name "Barchester" --company-id 364995 --scrape

Config comes from .env / .env.local (run `pb_doctor.py` first to check it):
  PHANTOMBUSTER_API_KEY, PB_SEARCH_EXPORT_AGENT, PB_SOURCE_AGENT, APP_SECRETS_KEY,
  DATABASE_URL.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

import enrich_linkedin
from model import Provider, User, db
from resolve_company_id import verify_match

logger = logging.getLogger("run_extraction")

DEFAULT_DATABASE_URL = "postgresql://darwinist:darwinist@localhost:5432/darwinist"
DEFAULT_EMAIL = "rob@shape.build"


class _CapturingClient:
    """Wraps phantombuster-lib's Phantombuster to record the raw result rows as they
    pass through, so the driver can print the true `result.json` field names (the
    spike's #1 open question) alongside the ingested rows. Delegates everything else."""

    def __init__(self, inner):
        self._inner = inner
        self.last_result: list[dict] | None = None

    def get_result(self, container_id):
        self.last_result = self._inner.get_result(container_id)
        return self.last_result

    def __getattr__(self, name):
        return getattr(self._inner, name)


def find_or_create_user(session: Session, email: str, api_key: str) -> User:
    """The User the phantom runs as (its id anchors the PhantomRun audit row). The
    Phantombuster key is stored encrypted; the LinkedIn session stays Phantombuster's
    (ADR 0016 amendment 2026-07-05)."""
    user = session.query(User).filter_by(email=email).one_or_none()
    if user is None:
        user = User(name=email.split("@")[0], email=email)
        session.add(user)
    user.phantombuster_api_key = api_key
    session.flush()
    return user


def pick_provider(session: Session, *, provider_id: int | None, name: str | None) -> Provider:
    """Resolve the target provider from an id or a name substring (first active match)."""
    if provider_id is not None:
        provider = session.get(Provider, provider_id)
        if provider is None:
            sys.exit(f"no provider with id {provider_id}")
        return provider
    q = (session.query(Provider)
         .filter(Provider.active.is_(True), Provider.name.ilike(f"%{name}%"))
         .order_by(Provider.id))
    provider = q.first()
    if provider is None:
        sys.exit(f"no active provider matching name ~ {name!r}")
    return provider


def resolve(pb, session: Session, provider: Provider, *, force: bool) -> None:
    """Run the live resolver on the provider's name, verify the match against our own
    website/town, and store the LinkedIn company id (unless `force`, which stores an
    unverified match — for eyeballing during a test)."""
    from resolver import resolve_ephemeral

    # NB this searches on provider.name (the CQC-registered legal name from the bulk
    # CSV), whereas the production path (resolve_company_id.resolve_provider) searches
    # on the CQC Syndication `brandName` — the trading name LinkedIn actually lists.
    # So a green result here is a weaker approximation of the real resolver; a
    # provider whose brand differs from its legal name may resolve differently at
    # scale. This tool deliberately takes no CQC key, hence the legal name.
    term = provider.name
    logger.info("resolving %r (CQC legal name, not brandName) via the resolver…", term)
    run = resolve_ephemeral(pb, term)
    li = run.result[0] if run.result else None
    if not li or not li.get("companyId"):
        sys.exit(f"resolver returned no company for {term!r}")

    verified, reason = verify_match(
        li, brand_name=term, website=provider.website, town=provider.town_city)
    print(f"  resolver → companyId={li.get('companyId')} "
          f"name={li.get('name')!r} vanity={li.get('vanity')!r}")
    print(f"  verify_match → {'PASS' if verified else 'REJECT'}: {reason}")
    if not verified and not force:
        sys.exit("match rejected — re-run with --force to store it anyway for testing")
    provider.linkedin_company_id = str(li["companyId"])
    session.flush()
    print(f"  stored provider.linkedin_company_id = {provider.linkedin_company_id}")


def scrape(pb, session: Session, user: User, provider: Provider, agent_id: str) -> None:
    """Run the Search Export for the provider's resolved company and report the run,
    the raw result field names, and the ingested Person/Role rows."""
    client = _CapturingClient(pb)
    logger.info("launching Search Export (agent %s) for company %s…",
                agent_id, provider.linkedin_company_id)
    run = enrich_linkedin.run_company_people(session, user, provider, agent_id, client=client)
    session.commit()

    print(f"\n  run {run.id}: status={run.status} credits={run.credits_spent} "
          f"error={run.error!r}")
    rows = client.last_result or []
    print(f"  raw result rows: {len(rows)}")
    if rows:
        print(f"  raw field names: {sorted(rows[0].keys())}")
        print(f"  sample row: {json.dumps(rows[0], indent=2)[:600]}")

    from model import Person, Role
    people = session.query(Person).filter(Person.linkedin_url.isnot(None)).all()
    print(f"\n  ingested: {len(people)} LinkedIn person(s)")
    for p in people[:10]:
        r = session.query(Role).filter_by(person_id=p.id).first()
        print(f"    - {p.name!r}  conf={p.match_confidence}  "
              f"role={(r.role_type if r else None)!r}  {p.linkedin_url}")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_extraction", description=__doc__.splitlines()[0])
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--provider-id", type=int, help="target provider by id")
    g.add_argument("--provider-name", help="target provider by name substring (first active match)")
    p.add_argument("--company-id", help="set linkedin_company_id directly, skipping the resolver")
    p.add_argument("--resolve", action="store_true", help="run the live resolver (spends credits)")
    p.add_argument("--scrape", action="store_true",
                   help="run the Search Export people scrape (spends credits, real personal data)")
    p.add_argument("--force", action="store_true", help="store an unverified resolver match")
    p.add_argument("--email", default=DEFAULT_EMAIL, help="User to run as (find-or-create)")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    args = build_parser().parse_args(argv)

    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    api_key = os.environ.get("PHANTOMBUSTER_API_KEY")
    agent_id = os.environ.get("PB_SEARCH_EXPORT_AGENT")
    if (args.resolve or args.scrape) and not api_key:
        sys.exit("PHANTOMBUSTER_API_KEY required for a live run (see .env.local)")
    if args.scrape and not agent_id:
        sys.exit("PB_SEARCH_EXPORT_AGENT required for --scrape")

    engine = create_engine(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    db.metadata.create_all(engine)

    pb = None
    if args.resolve or args.scrape:
        from phantombuster import Phantombuster
        pb = Phantombuster(api_key)

    with Session(engine) as session:
        provider = pick_provider(session, provider_id=args.provider_id, name=args.provider_name)
        print(f"provider #{provider.id}: {provider.name!r} "
              f"(town={provider.town_city!r}, website={provider.website!r})")
        print(f"  current linkedin_company_id: {provider.linkedin_company_id!r}")

        if args.company_id:
            provider.linkedin_company_id = args.company_id
            session.flush()
            print(f"  set linkedin_company_id = {args.company_id}")
        elif args.resolve:
            resolve(pb, session, provider, force=args.force)

        if not args.scrape:
            session.commit()
            print("\n(dry) no scrape run. Add --scrape to pull people "
                  "(spends credits, real personal data).")
            return 0

        if not provider.linkedin_company_id:
            sys.exit("provider has no linkedin_company_id — pass --resolve or --company-id")
        user = find_or_create_user(session, args.email, api_key)
        scrape(pb, session, user, provider, agent_id)
    return 0


if __name__ == "__main__":
    sys.exit(main())
