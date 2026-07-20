"""Resolve a CQC provider to its LinkedIn numeric company id (ADR 0016 PWS2).

The trading **brandName** (not the Companies House legal name) is what LinkedIn
lists, so we search on it via phantombuster-lib's resolver, then **verify** the
returned company against a CQC signal before trusting it — LinkedIn company search
is fuzzy and will happily return an unrelated firm (the canonical failure is
`Scarborough Hall → rbrecycling`). Only a verified match is stored on
`Provider.linkedin_company_id`.

Verification, strongest first:
1. **website domain** — if both the CQC record and the LinkedIn About page carry a
   website, their domains must agree (a mismatch is a hard reject);
2. **name similarity** — the LinkedIn company name must overlap the brand we
   searched for (this is what rejects `rbrecycling`);
3. **town** — the CQC town appearing in the LinkedIn HQ line corroborates.

One brand → many CQC providers (`brandId` groups siblings), so a batch caches
`brandId → companyId` and reuses it rather than re-running the (paid) resolver.

The live resolver call needs a Phantombuster key + a LinkedIn session and so is
gated (`live_resolver`); the decision logic (`verify_match`, `resolve_provider`)
is pure/injectable and unit-tested offline.
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from dataclasses import dataclass
from typing import Callable
from urllib.parse import urlparse

import time

from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session

from cqc import CQC  # phantombuster-lib's CQC Syndication client
from resolver import search_term  # phantombuster-lib
from model import Provider, Role, db

logger = logging.getLogger(__name__)

# A resolver takes a search term and returns the LinkedIn result row (a dict with
# at least `companyId`, `name`, `website`, `headquarters`), or None for no match.
Resolver = Callable[[str], dict | None]

# Company-name noise dropped before comparing (legal/branding boilerplate).
_NAME_STOPWORDS = frozenset({
    "the", "brand", "ltd", "limited", "llp", "plc", "group", "holdings",
    "care", "healthcare", "services", "uk", "and", "co", "company",
})


def _domain(url: str | None) -> str | None:
    """Bare registrable-ish host of a URL: scheme/`www.`/path stripped, lowercased.
    `https://www.Practice-Plus.com/x` → `practice-plus.com`."""
    if not url:
        return None
    url = url.strip()
    if "//" not in url:
        url = "//" + url  # let urlparse find the netloc when there's no scheme
    host = (urlparse(url).netloc or "").lower()
    if host.startswith("www."):
        host = host[4:]
    return host or None


def _name_tokens(name: str | None, *, keep_stopwords: bool = False) -> set[str]:
    """Significant lowercase word tokens of a company name (boilerplate removed).
    `keep_stopwords` retains the boilerplate — used only as a fallback for names that
    are *entirely* boilerplate (see `_names_similar`)."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if len(w) > 1 and (keep_stopwords or w not in _NAME_STOPWORDS)}


def _names_similar(a: str | None, b: str | None) -> bool:
    """True if two company names share enough significant tokens to be the same
    firm — Jaccard ≥ 0.5, or one token set contained in the other."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb:
        # A name made entirely of boilerplate — common in this sector ('Care UK' →
        # care, uk both stopwords). Fall back to the full word sets so a real
        # all-boilerplate brand still matches instead of being silently dropped.
        ta, tb = _name_tokens(a, keep_stopwords=True), _name_tokens(b, keep_stopwords=True)
    if not ta or not tb:
        return False
    inter = ta & tb
    if not inter:
        return False
    if ta <= tb or tb <= ta:
        return True
    return len(inter) / len(ta | tb) >= 0.5


def verify_match(
    li: dict, *, brand_name: str | None = None,
    website: str | None = None, town: str | None = None,
) -> tuple[bool, str]:
    """Decide whether a LinkedIn resolver row is really the CQC provider.
    Returns (verified, human-readable reason)."""
    cqc_domain, li_domain = _domain(website), _domain(li.get("website"))
    if cqc_domain and li_domain:
        if cqc_domain == li_domain:
            return True, f"website domain match ({cqc_domain})"
        return False, f"website domain mismatch (cqc {cqc_domain} vs linkedin {li_domain})"

    if _names_similar(brand_name, li.get("name")):
        hq = li.get("headquarters") or ""
        if town and hq and town.lower() not in hq.lower():
            return False, (
                f"name matches but town does not (cqc {town!r} not in linkedin HQ {hq!r})"
            )
        detail = f"; town {town!r} in HQ" if (town and town.lower() in (hq or "").lower()) else ""
        return True, f"name match ({brand_name!r} ≈ {li.get('name')!r}){detail}"

    return False, (
        f"no confirming signal (brand {brand_name!r} vs linkedin name {li.get('name')!r}; "
        "no website domain overlap)"
    )


@dataclass(frozen=True)
class ResolveOutcome:
    """The result of trying to resolve one provider. `company_id` is set only when
    `status == "resolved"` or `"cached"`."""

    status: str  # resolved | cached | rejected | no-match | no-term | skipped
    reason: str
    search_term: str | None = None
    company_id: str | None = None


def resolve_provider(
    provider: Provider, *, cqc_client, resolve: Resolver,
    cache: dict[str, str] | None = None,
) -> ResolveOutcome:
    """Resolve one provider to a verified LinkedIn company id and store it on the
    `Provider` (it's session-attached, so setting the attribute is enough; caller
    commits). `cqc_client` is phantombuster-lib's CQC client
    (for brandName/brandId/website/town); `resolve` runs the LinkedIn resolver.
    Both are injected so this is testable without network. `cache` (brandId →
    companyId), when given, short-circuits siblings under the same brand."""
    cqc_prov = cqc_client.get_provider(provider.cqc_provider_id)

    if cqc_prov.get("registrationStatus") == "Deregistered":
        return ResolveOutcome("skipped", "provider deregistered")

    brand_id = cqc_prov.get("brandId")
    if cache is not None and brand_id and brand_id in cache:
        provider.linkedin_company_id = cache[brand_id]
        return ResolveOutcome("cached", f"brand {brand_id} already resolved",
                              company_id=cache[brand_id])

    term = search_term(cqc_prov)
    if not term:
        return ResolveOutcome("no-term", "provider has no brandName or name")

    li = resolve(term)
    if not li or not li.get("companyId"):
        return ResolveOutcome("no-match", "resolver returned no company", search_term=term)

    verified, reason = verify_match(
        li, brand_name=term,
        website=cqc_prov.get("website") or provider.website,
        town=cqc_prov.get("postalAddressTownCity") or provider.town_city,
    )
    if not verified:
        return ResolveOutcome("rejected", reason, search_term=term)

    company_id = str(li["companyId"])
    provider.linkedin_company_id = company_id
    if cache is not None and brand_id:
        cache[brand_id] = company_id
    return ResolveOutcome("resolved", reason, search_term=term, company_id=company_id)


# --- No-auth resolver (the default: public company page, no session, no credits) --


def public_resolver() -> Resolver:
    """A `Resolver` that resolves a name to its LinkedIn company id from the **public**
    company page — no login, no Phantombuster, no credits (ADR 0016 PWS2; spike:
    linkedin-acquisition). Tries slug candidates most-specific-first and returns the
    first whose page name matches the term, so a short slug hitting an unrelated firm
    is skipped; `verify_match` downstream is the second gate."""
    from linkedin_public import fetch_company, slug_candidates

    def _resolve(term: str) -> dict | None:
        for slug in slug_candidates(term):
            page = fetch_company(slug)
            if page and _names_similar(term, page.get("name")):
                return page
        return None

    return _resolve


# --- Gated live driver (the phantom path — flaky JS search; kept as a fallback) ---


def live_resolver(pb, *, timeout: float = 280, poll: float = 6) -> Resolver:
    """Build a `Resolver` that runs phantombuster-lib's ephemeral resolver phantom.
    Not exercised offline — it launches a real LinkedIn scrape. The LinkedIn session
    is managed inside Phantombuster (ADR 0016 amendment 2026-07-05), so no cookie is
    passed from here."""
    from resolver import resolve_ephemeral  # imported lazily so offline tests need no pb

    def _resolve(term: str) -> dict | None:
        run = resolve_ephemeral(pb, term, timeout=timeout, poll=poll)
        return run.result[0] if run.result else None

    return _resolve


def resolve_all(session, cqc_client, *, resolve: Resolver | None = None,
                limit: int | None = None, sleep: float = 0.0,
                richest_first: bool = False) -> dict[str, int]:
    """Batch driver: resolve every active provider still missing a
    linkedin_company_id, caching by brand. Caller commits. Defaults to the no-auth
    `public_resolver` (no Phantombuster); `cqc_client` still supplies the trading
    brandName that makes the slug guess accurate. Pass `resolve=live_resolver(pb)`
    to use the phantom path instead.

    `sleep` paces LinkedIn — a delay after each provider whose outcome actually hit
    the resolver (not cache/skip), since even a residential IP throttles at volume.
    `richest_first` orders by CH-role count desc, so a capped run resolves the
    decision-maker-rich chains (the best demo/outreach targets) before the long tail
    of small independents."""
    resolve = resolve or public_resolver()
    cache: dict[str, str] = {}
    q = (session.query(Provider)
         .filter(Provider.active.is_(True),
                 Provider.cqc_provider_id.isnot(None),
                 Provider.linkedin_company_id.is_(None)))
    if richest_first:
        role_ct = (select(func.count(Role.id))
                   .where(Role.provider_id == Provider.id).scalar_subquery())
        q = q.order_by(role_ct.desc())
    if limit:
        q = q.limit(limit)

    _hit_resolver = {"resolved", "rejected", "no-match"}  # outcomes that fetched LinkedIn
    tally: dict[str, int] = {}
    for provider in q:
        try:
            outcome = resolve_provider(provider, cqc_client=cqc_client,
                                       resolve=resolve, cache=cache)
        except Exception as exc:  # noqa: BLE001 — one flaky provider (a CQC 5xx, a
            # timeout, a LinkedIn hiccup) must not abort a 37k-row batch. The CQC
            # call is resolve_provider's first step, before any write, so nothing is
            # half-applied; tally it and carry on.
            tally["error"] = tally.get("error", 0) + 1
            logger.warning("provider %s (%s): error — %s: %s",
                           provider.id, provider.name, type(exc).__name__, exc)
            continue
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
        logger.info("provider %s (%s): %s — %s", provider.id, provider.name,
                    outcome.status, outcome.reason)
        if sleep and outcome.status in _hit_resolver:
            time.sleep(sleep)
    return tally


def cqc_key() -> str | None:
    """The CQC Syndication subscription key: primary, falling back to secondary.
    These are the canonical names in `.env.example` and match CQC's developer
    portal ('primary'/'secondary' keys)."""
    return os.getenv("CQC_PRIMARY_KEY") or os.getenv("CQC_SECONDARY_KEY")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resolve_company_id",
        description="Resolve CQC providers to verified LinkedIn company ids.",
    )
    p.add_argument("--limit", type=int, default=None, help="cap providers processed")
    p.add_argument("--sleep", type=float, default=0.0,
                   help="seconds to pause after each LinkedIn fetch (pace to avoid "
                        "throttling; e.g. 1.5)")
    p.add_argument("--richest-first", action="store_true",
                   help="resolve decision-maker-rich providers (most CH roles) first "
                        "— the best demo/outreach targets, ahead of the long tail")
    p.add_argument("--dry-run", action="store_true",
                   help="resolve + log outcomes but roll back (write nothing)")
    p.add_argument("--emit-changeset", action="store_true",
                   help="also write this run's resolutions to "
                        "data/changes/linkedin-resolver-<date>.json for ADR-0015 replay "
                        "onto another DB (e.g. the box, which LinkedIn authwalls)")
    return p


def emit_changeset(resolutions: list[dict], changes_dir: str = "data/changes") -> str:
    """Write/merge this run's {cqc_provider_id, linkedin_company_id} resolutions into
    a dated linkedin-resolver file (one per day; same-day re-runs accumulate, new id
    wins). Non-personal company ids, so plaintext-in-git is fine (ADR 0015/0019).
    Returns the path."""
    import datetime as dt
    import json
    from pathlib import Path

    d = Path(changes_dir)
    d.mkdir(parents=True, exist_ok=True)
    path = d / f"linkedin-resolver-{dt.date.today().isoformat()}.json"
    merged: dict[str, str] = {}
    if path.exists():
        for e in json.loads(path.read_text(encoding="utf-8")).get("resolved", []):
            merged[e["cqc_provider_id"]] = str(e["linkedin_company_id"])
    for e in resolutions:
        merged[e["cqc_provider_id"]] = str(e["linkedin_company_id"])
    payload = {"resolved": [{"cqc_provider_id": k, "linkedin_company_id": v}
                            for k, v in sorted(merged.items())]}
    path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    return str(path)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    from dotenv import load_dotenv
    load_dotenv()
    load_dotenv(".env.local", override=True)

    key = cqc_key()
    if not key:
        sys.exit("CQC_PRIMARY_KEY (or CQC_SECONDARY_KEY) is required — the CQC "
                 "brandName lookup feeds the resolver. Get one at "
                 "https://api-portal.service.cqc.org.uk/. The LinkedIn resolver "
                 "itself is no-auth (no Phantombuster).")

    database_url = os.getenv(
        "DATABASE_URL", "postgresql://darwinist:darwinist@localhost:5432/darwinist"
    )
    engine = create_engine(database_url)
    db.metadata.create_all(engine)
    cqc_client = CQC(key, partner_code=os.getenv("CQC_PARTNER_CODE"))

    with Session(engine) as session:
        # Snapshot resolved ids before, so we can emit only THIS run's new ones.
        before = dict(
            session.query(Provider.cqc_provider_id, Provider.linkedin_company_id)
            .filter(Provider.linkedin_company_id.isnot(None)).all()
        ) if args.emit_changeset else {}

        tally = resolve_all(session, cqc_client, limit=args.limit,
                            sleep=args.sleep, richest_first=args.richest_first)

        if args.dry_run:
            session.rollback()
            logger.info("dry-run: rolled back (nothing written)")
        else:
            session.commit()
            if args.emit_changeset:
                after = dict(
                    session.query(Provider.cqc_provider_id, Provider.linkedin_company_id)
                    .filter(Provider.linkedin_company_id.isnot(None)).all()
                )
                new = [{"cqc_provider_id": pid, "linkedin_company_id": cid}
                       for pid, cid in sorted(after.items()) if before.get(pid) != cid]
                path = emit_changeset(new)
                logger.info("emitted %d resolution(s) -> %s", len(new), path)
    logger.info("done: %s", tally)
    return 0


if __name__ == "__main__":
    main()
