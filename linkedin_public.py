"""No-auth LinkedIn company-id resolution (ADR 0016 PWS2).

Resolves a company brand/legal name to its LinkedIn numeric company id by fetching
the **public** company page — no login, no Phantombuster, no credits. The numeric id
sits in the public HTML as `urn:li:organization:<N>`; the `<title>` gives the name
and the JSON-LD address gives the HQ. Deterministic where the phantom's JS-search
scrape was flaky (see [the acquisition spike](docs/spikes/linkedin-acquisition-approach.md)).

This is the transport + parse layer. `resolve_company_id.public_resolver()` wraps it
as the injected `Resolver` (adding the name-match gate), so the rest of PWS2 is
unchanged. No website is returned: the public page's `sameAs` is often a
careers/marketing subdomain (e.g. `barchestercareers.com` vs the provider's
`barchester.com`), an unreliable domain signal that would false-reject in
`verify_match` — verification leans on name + HQ instead.
"""

from __future__ import annotations

import logging
import re
import urllib.error
import urllib.request

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36")

# The numeric company id, in rough order of how it appears in the public HTML.
_ID_PATTERNS = [re.compile(p) for p in (
    r"urn:li:fsd_company:(\d+)", r"urn:li:organization:(\d+)",
    r"urn:li:company:(\d+)", r'"companyId":(\d+)', r"[?&]f_C=(\d+)")]

# Trailing legal-form words to drop before guessing a slug (LinkedIn uses the brand).
_LEGAL_SUFFIXES = frozenset({
    "limited", "ltd", "plc", "llp", "llc", "inc", "corporation", "corp",
    "group", "holdings", "co",
})

_MAX_CANDIDATES = 6  # cap the public-page fetches per resolution


def slug_candidates(name: str) -> list[str]:
    """LinkedIn company-slug guesses for a name, most specific first. Drops trailing
    legal-form words, then progressively drops trailing tokens, so
    'Barchester Healthcare Homes Limited' yields
    barchester-healthcare-homes → barchester-healthcare → barchester."""
    words = re.findall(r"[a-z0-9]+", (name or "").lower())
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    # Distinct-length prefixes (words hold no hyphens), so no dedup needed.
    out = ["-".join(words[:n]) for n in range(len(words), 1, -1)]  # full → 2 tokens
    if words:
        out.append(words[0])                    # single-token brands (e.g. 'bupa')
    return out[:_MAX_CANDIDATES]


def _one(pattern: str, html: str) -> str | None:
    m = re.search(pattern, html)
    return m.group(1).strip() if m else None


def _fetch(url: str) -> str | None:
    """GET a URL with a browser UA; None on 404 / network error."""
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.read().decode("utf-8", "ignore")
    except urllib.error.HTTPError as e:
        if e.code != 404:
            logger.warning("%s -> HTTP %s", url, e.code)
        return None
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        logger.warning("%s -> %s", url, e)
        return None


def fetch_company(slug: str) -> dict | None:
    """Fetch the public company page for `slug` and extract id + name + HQ. Returns a
    dict shaped for `verify_match` (`companyId`, `name`, `headquarters`, `vanity`), or
    None if the page doesn't exist or carries no company id."""
    html = _fetch(f"https://www.linkedin.com/company/{slug}/")
    if not html:
        return None
    company_id = None
    for pattern in _ID_PATTERNS:
        m = pattern.search(html)
        if m:
            company_id = m.group(1)
            break
    if not company_id:
        return None
    name_m = re.search(r"<title>(.*?)\s*\|\s*LinkedIn</title>", html)
    locality = _one(r'"addressLocality":"([^"]+)"', html)
    region = _one(r'"addressRegion":"([^"]+)"', html)
    hq = ", ".join(x for x in (locality, region) if x) or None
    return {
        "companyId": company_id,
        "name": name_m.group(1).strip() if name_m else None,
        "headquarters": hq,
        "vanity": slug,
        "linkedinUrl": f"https://www.linkedin.com/company/{slug}/",
    }
