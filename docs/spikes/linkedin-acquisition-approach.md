# Spike — LinkedIn acquisition: Puppeteer API vs Phantombuster store phantom

**Status:** Resolved (2026-07-06), with an important **correction 2026-07-06**
(see below): the "cookie injection is a dead end" reading of Exp 1 was contaminated
by a stale session and is **retracted**. Net finding stands — the no-auth id lookup
(Exp 3) is the simplest resolver and the store Search Export (Exp 2) scrapes people —
but *not* because cookie injection fails.

> **Correction (2026-07-06) — the custom resolver is NOT broken.** Every early test
> of the custom resolver ran against the **stale** session (the empty `search ->
> vanity:` results, and Exp 1's local-Puppeteer login wall, all used the same expired
> `li_at`). Re-run **after** the session was reconnected, the resolver's **search step
> works**: `search -> vanity: barchester-healthcare`. So injecting `li_at` alone *does*
> authenticate LinkedIn — Exp 1's "dead end" was just a stale cookie. The resolver now
> fails one step later, at the About-page scrape, with `net::ERR_ABORTED` — a fixable
> navigation error, and the `companyId` it's after is exactly what Exp 3 gets from the
> public page with no auth. **Consequence for the decision:** choosing the no-auth
> resolver over fixing the custom phantom is a call on *simplicity* (no session, no
> credits, no scrape fragility), not because the phantom is broken. The rest of this
> doc predates the correction; read Exp 1's conclusion through this lens.

**Question.** Our custom Phantombuster *resolver* phantom (company name → LinkedIn
numeric id, PWS2) was returning **nothing** — an empty company-search page
(`search -> vanity:` blank, exit 0). We *first* read this as a datacenter-IP wall;
it turned out to be a **stale session** (corrected above). This spike asks: **what
acquisition mechanism do we commit to** — is driving Puppeteer *ourselves* better
than a custom Phantombuster phantom, and where does the maintained *store* phantom sit?

See the ADR-0016 direction and the [phantombuster-api spike](phantombuster-api.md)
for the transport facts already established.

## The reframe

The choice is **not** "phantombuster-lib's Puppeteer vs the Puppeteer API" — it is
the same Puppeteer either way. The real axis is **who provides anti-detection**:
residential proxies, full session management, fingerprint/behaviour evasion, and
ongoing maintenance against LinkedIn's changes. Puppeteer is a commodity; not
getting blocked is the moat.

Three candidates, and the hypothesis for each:

| Path | What it is | Hypothesis |
|---|---|---|
| **A. Store phantom** | Lease Phantombuster's maintained Search Export (their proxies + session) | Works; it's what the credits buy. Gated on ADR 0017 (real PII). |
| **B. Self-hosted Puppeteer** | We run headless Chrome ourselves + the session cookie | Works from a *residential* IP; hits the same wall as the custom phantom from a *datacenter* IP. So only viable behind residential proxies — i.e. we'd own LinkedIn's blocking war. |
| **C. Custom Phantom (current resolver)** | Our Puppeteer script run inside Phantombuster | Worst of both — pay credits + blind, and (unconfirmed) may get **no** proxy/session treatment, since that lives in the *store* phantoms. This is what's failing now. |

A fourth option sidesteps the whole fight for the *resolver* specifically:

| **D. No-auth id lookup** | Resolve company id without a logged-in scrape (public company page / web search) | The numeric id is often in public HTML/og-tags; resolution is a far lighter problem than people-scraping. |

## Experiments

### Exp 1 — Puppeteer API from a residential IP (Path B), non-PII

Run headless Puppeteer **locally** (residential IP) with the borrowed `li_at`,
navigate LinkedIn, and dump title + final URL. Isolates the variable the datacenter
phantom couldn't: **is it the IP or the cookie?**

**Result (2026-07-06): the cookie — but a STALE one (see correction).** Injecting the
borrowed `li_at` into a fresh local (residential-IP) Chromium and hitting `/feed/`:
- domain `.linkedin.com` → `ERR_TOO_MANY_REDIRECTS` (login redirect loop).
- domain `.www.linkedin.com` (exactly how the resolver phantom sets it) → lands on
  **`/login/?session_redirect=…`**, title *"LinkedIn Login, Sign in"*.

**⚠️ Retracted conclusion.** At the time I read this as "cookie injection does not
authenticate, full stop." That was wrong: **this test used the expired `li_at`** (the
same stale cookie that made Exp 2 fail with "Session cookie not valid anymore"). After
the session was reconnected, the custom resolver — which injects `li_at` exactly the
same way — **authenticated the LinkedIn company search** (`search -> vanity:
barchester-healthcare`). So a *fresh* `li_at` injection **does** work; this probe only
proved a *stale* one doesn't. Self-hosted Puppeteer for authenticated LinkedIn is
therefore **not** a proven dead end — this experiment should be re-run with a fresh
cookie before drawing that conclusion.

### Exp 2 — Store Search Export (Path A), PII-gated

Run the store Search Export via API against a **known** company id (from Exp 1 or
supplied), bounded to a small result count. Confirms (a) the store phantom returns
people, (b) the connected session is healthy, (c) the raw `result.json` field
names (the [phantombuster-api spike](phantombuster-api.md)'s #1 open item). Gated
on ADR 0017 (real personal data) — needs explicit go-ahead.

**Result:** _(pending)_

### Exp 3 — No-auth id lookup (Path D), non-PII

Fetch the public LinkedIn company page for the brand and extract the numeric id
without any logged-in session.

**Result (2026-07-06): works, and is trivially reliable for brands with a LinkedIn
page.** A plain `curl` (browser UA, **no cookie**) of
`https://www.linkedin.com/company/<slug>/` returns HTTP 200 with the id in the HTML
as `urn:li:organization:<N>`, plus `<title>Brand | LinkedIn</title>` as a built-in
verification signal. Slug = `name.lower().replace(" ", "-")` resolved **6/6** test
brands first try:

| Brand | slug | companyId |
|---|---|---|
| Barchester Healthcare | `barchester-healthcare` | 80128 |
| HC-One | `hc-one` | 2851202 |
| Care UK | `care-uk` | 473831 |
| Bupa | `bupa` | 110750981 |
| Four Seasons Health Care | `four-seasons-health-care` | 2177066 |
| Sanctuary Care | `sanctuary-care` | 5045635 |

Caveats: (a) tested from a residential IP — LinkedIn may throttle/authwall public
pages from datacenter IPs at volume, so a production batch wants rate-limiting and
possibly residential egress; (b) these are large national brands with clean pages —
most of the ~37k CQC providers are small (single homes, sole traders) with **no**
LinkedIn company page or a slug that won't match the CQC legal name, so real-dataset
hit-rate will be far lower (that's fine — only providers with a presence are outreach
targets); (c) slug-guess needs a fallback (web search, or the CQC website domain) +
title/website verification (reuse `verify_match`) to avoid wrong matches.

### Exp 2 — Store Search Export (Path A), PII-gated

**Result (2026-07-06): our code drives it correctly; blocked only by a stale
Phantombuster↔LinkedIn session.** Ran the store Search Export for Barchester
(company 80128, `--limit 10`). The phantom's own log confirms our per-run argument
landed exactly right:

```
ℹ️ Input: .../search/results/people/?currentCompany=["80128"]
ℹ️ Number of results to scrape per launch: 10
🔄 Connecting to LinkedIn...
❌ Session cookie not valid anymore. Please log in to LinkedIn to get a new one.
Process finished with an error (exit code: 84)
```

So the `linkedInSearchUrl` key + bonus-argument merge + `--limit` all work. **Zero
people because the connected LinkedIn session has expired** — the same root cause as
Exp 1's cookie failure: the session stored on the agent is stale. Fix is operational,
not code: **reconnect LinkedIn in the Phantombuster UI** (browser extension), then
re-run. Session expiry is a recurring operational fact (this is the "re-auth a stale
LinkedIn session" Task in [product-vision](../product-vision.md)).

Also surfaced **two real bugs in our success gate**, now fixed + regression-tested:
`run_identification_phantom` hard-gated on `lastEndStatus == "success"`, but store
phantoms finish with `lastEndStatus None`; and a "finished" container can still be a
failure (`exitCode 84`). The gate now keys on **exitCode == 0** (matching the lib's
own `RunResult.succeeded`).

**Re-run after reconnecting the session (2026-07-06): full success.** Once the
agent's identity cookie fingerprint actually changed (the reconnect had to write
through to *this* agent's identity, not just the central account — a fiddly
Phantombuster UI step), the scrape returned **10 real Barchester people**, ingested
as low-confidence Person + Role, `linkedin_url`-deduped, none merged into CH
directors. The full PWS2→PWS3 consumer path works end to end against live data.

**Confirmed result field names** (the phantombuster-api spike's #1 open item — the
Search Export `result.json`, not the Employees Export):

```
additionalInfo, category, company, company2, companyId, companySlug, companyUrl,
companyUrl2, connectionDegree, firstName, fullName, headline, industry,
jobDateRange, jobTitle, lastName, linkedinProfileUrl, location, profileImageUrl,
profileUrl, query, school, schoolDegree, searchAccountFullName, timestamp, vmid
```

Our tolerant `parse_profile` handled them correctly with no change: `fullName`→name,
`headline`→the stored headline (in `Role.control_nature`), `profileUrl`→`linkedin_url`.
Note the Search Export title field is **`jobTitle`/`headline`**, not the Employees
Export's `job` — our candidate-key list already covered both. The `companyId` /
`companySlug` fields are a future cross-check against the resolved company id.

Caveat for outreach: the Search Export returns **all** current-company people
unranked (a Kitchen Assistant next to a Head of IT Operations), so title-based
seniority filtering/ranking is a downstream concern, not the scraper's job.

## Findings

1. **Cookie injection works with a *fresh* session** (Exp 1, corrected). The early
   "lifted `li_at` bounces to `/login`" result used the **stale** cookie; re-run after
   reconnecting, the resolver authenticated the LinkedIn company search. So injecting
   `li_at` is **not** a dead end. The custom resolver's remaining failure is a later,
   fixable `net::ERR_ABORTED` on the About-page scrape — not an auth wall.
2. **Company-id resolution needs no auth at all** (Exp 3). The numeric id is in the
   public page HTML; slug-guess + a title/website verify resolves it for free — no
   Puppeteer, no Phantombuster, no credits, no anti-detection war. Even the custom
   resolver's *failing* step (get the id off the authed About page) is redundant with
   this. **This is the simplest resolver, and the reason to prefer it — not that the
   phantom is broken.**
3. **People scraping needs the managed authenticated session** — a live scrape at
   volume — so it stays leased on the store Search Export (Exp 2 confirmed it returns
   rows + the field names, once the session is fresh).

## Decision input (feeds the ADR)

The split: **own the no-auth resolution (for simplicity), lease the authed
people-scrape.**

- **Prefer** an in-repo **no-auth resolver** for PWS2: brand → slug/search → public
  page → `urn:li:organization:<N>` → `verify_match`. Not because the custom phantom
  fails (it mostly works with a fresh session — see the correction), but because the
  no-auth path needs **no session, no credits, no scrape fragility** for a problem
  that's a plain HTTP GET. (Alternatively, the custom phantom's About-page step could
  be fixed or routed through the public page — a live option, chosen 2026-07-06, being
  implemented.)
- **Keep** the store **Search Export** (PWS3) for people — a live authenticated scrape
  is genuinely needed there.
- This should become an amendment to [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md)
  and reshape the [plan](../plans/linkedin-ingestion.md)'s PWS2.
