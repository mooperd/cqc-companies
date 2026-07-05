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

from resolver import search_term  # phantombuster-lib
from model import Provider

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


def _name_tokens(name: str | None) -> set[str]:
    """Significant lowercase word tokens of a company name (boilerplate removed)."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    return {w for w in words if w not in _NAME_STOPWORDS and len(w) > 1}


def _names_similar(a: str | None, b: str | None) -> bool:
    """True if two company names share enough significant tokens to be the same
    firm — Jaccard ≥ 0.5, or one token set contained in the other."""
    ta, tb = _name_tokens(a), _name_tokens(b)
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


# --- Gated live driver (needs a Phantombuster key + LinkedIn session) ----------


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


def resolve_all(session, cqc_client, pb, *, limit: int | None = None) -> dict[str, int]:
    """Gated batch driver: resolve every active provider still missing a
    linkedin_company_id, caching by brand. Caller commits. Needs live keys."""
    resolve = live_resolver(pb)
    cache: dict[str, str] = {}
    q = (session.query(Provider)
         .filter(Provider.active.is_(True),
                 Provider.cqc_provider_id.isnot(None),
                 Provider.linkedin_company_id.is_(None)))
    if limit:
        q = q.limit(limit)

    tally: dict[str, int] = {}
    for provider in q:
        outcome = resolve_provider(provider, cqc_client=cqc_client,
                                   resolve=resolve, cache=cache)
        tally[outcome.status] = tally.get(outcome.status, 0) + 1
        logger.info("provider %s (%s): %s — %s", provider.id, provider.name,
                    outcome.status, outcome.reason)
    return tally


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="resolve_company_id",
        description="Resolve CQC providers to verified LinkedIn company ids.",
    )
    p.add_argument("--limit", type=int, default=None, help="cap providers processed")
    return p


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    build_parser().parse_args(argv)

    if not (os.environ.get("CQC_SUBSCRIPTION_KEY") and os.environ.get("PHANTOMBUSTER_API_KEY")):
        sys.exit("CQC_SUBSCRIPTION_KEY and PHANTOMBUSTER_API_KEY are required for a live run.")
    # Live wiring is intentionally left to the caller who has keys + a DB session;
    # this entrypoint only validates configuration (mirrors enrich_linkedin's gate).
    print("Configured. Wire a Session + call "
          "resolve_all(session, CQC(cqc_key), Phantombuster(pb_key)).")


if __name__ == "__main__":
    main()
