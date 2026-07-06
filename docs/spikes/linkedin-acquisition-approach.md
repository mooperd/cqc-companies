# Spike — LinkedIn acquisition: Puppeteer API vs Phantombuster store phantom

**Status:** Resolved (2026-07-06) for the architecture decision (Exp 1 + Exp 3
below). Exp 2 (store-phantom people scrape) remains a gated confirmation, not a
blocker for the decision.

**Question.** Our custom Phantombuster *resolver* phantom (company name → LinkedIn
numeric id, PWS2) runs clean but returns **nothing** — LinkedIn serves it an empty
company-search page (`search -> vanity:` blank, exit 0), from a Scaleway
datacenter container injecting only `li_at`. That is the signature of LinkedIn
walling a datacenter IP. This spike asks: **what acquisition mechanism do we
commit to** — and specifically, is driving Puppeteer *ourselves* better than a
custom Phantombuster phantom, and where does the maintained *store* phantom sit?

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

**Result (2026-07-06): the cookie, not the IP. Cookie injection does not
authenticate.** Injecting the borrowed `li_at` into a fresh local (residential-IP)
Chromium and hitting `/feed/`:
- domain `.linkedin.com` → `ERR_TOO_MANY_REDIRECTS` (login redirect loop).
- domain `.www.linkedin.com` (exactly how the resolver phantom sets it) → lands on
  **`/login/?session_redirect=…`**, title *"LinkedIn Login, Sign in"*.

So the resolver phantom's empty result was **not** primarily the datacenter IP — it
got the login/authwall (no `/company/` links → blank vanity), and it would fail the
same way anywhere. **You cannot lift `li_at` and drive your own browser with it** —
LinkedIn rejects a bare cookie in a foreign browser/session context. The store
phantoms work because they run *inside Phantombuster's managed, already-authenticated
session* (the extension "connect" establishes it), not by cookie injection. Our
custom resolver injects a cookie the same way this probe did, and dies the same way.

Corollary: **self-hosted Puppeteer for *authenticated* LinkedIn is a dead end**
unless you do a full interactive login in that browser and keep it warm — at which
point you're rebuilding Phantombuster's session infra.

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

## Findings

1. **Cookie injection is a dead end** (Exp 1). A lifted `li_at` does not authenticate
   a foreign browser — LinkedIn bounces to `/login`, regardless of IP. This kills
   both self-hosted Puppeteer *and* the custom Phantombuster resolver phantom for any
   **authenticated** LinkedIn action. The store phantoms work only because they run
   inside Phantombuster's managed authenticated session.
2. **Company-id resolution needs no auth at all** (Exp 3). The numeric id is in the
   public page HTML; slug-guess + a title/website verify resolves it for free — no
   Puppeteer, no Phantombuster, no credits, no anti-detection war. The custom resolver
   phantom was the heaviest possible tool for a problem that's a plain HTTP GET.
3. **People scraping genuinely needs the managed authenticated session** — it's the
   one thing cookie-injection can't replicate — so it stays leased on the store
   Search Export (pending Exp 2 to confirm it returns rows + the field names).

## Decision input (feeds the ADR)

The clean split: **own the no-auth resolution, lease the authed people-scrape.**

- **Replace** the phantombuster-lib custom *resolver* phantom (PWS2) with an in-repo
  **no-auth resolver**: brand → slug/search → public page → `urn:li:organization:<N>`
  → `verify_match`. Drops the resolver's cookie-borrow (`PB_SOURCE_AGENT`), the
  ephemeral-phantom credits, and the whole blocking problem we hit.
- **Keep** the store **Search Export** (PWS3) for people — do NOT self-host it and do
  NOT port it to a custom phantom; both inherit the Exp-1 authwall. This reverses the
  earlier "port Search Export into the lib like the resolver" idea: the resolver is
  the thing to pull *out* of a phantom, not the model to copy.
- This should become an amendment to [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md)
  and reshape the [plan](../plans/linkedin-ingestion.md)'s PWS2.
