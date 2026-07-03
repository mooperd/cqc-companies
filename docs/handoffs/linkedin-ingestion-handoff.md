# Handoff — LinkedIn ingestion (ADR 0016): PWS0+PWS1 done; PWS2 (resolve provider → linkedin_company_id) is next

**Created:** 2026-07-02 · **Updated:** 2026-07-03
**Working tree:** clean
**Branch:** main

<!--
Ephemeral. At resolution, fold load-bearing pieces into docs/plans/linkedin-ingestion.md
/ the ADRs and `git rm` this file. See docs/handoff-discipline.md.
-->

## Goal & next-up

**Goal of this session:** execute [ADR 0016](../adr/0016-linkedin-phantombuster-ingestion.md)
— LinkedIn identification of the ~69% of decision-makers Companies House can't see.
Built the offline store-phantom mechanism (WS1–3), proved it live end-to-end, then
**pivoted the acquisition mechanism to depend on
[`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib)** — a
more mature parallel implementation. Also drafted ADR 0017 (GDPR posture) and fixed
a security issue in phantombuster-lib.

**PWS0 is DONE** (2026-07-03, phantombuster-lib
[PR #2](https://github.com/mooperd/phantombuster-lib/pull/2), rebase-merged):
`pyproject.toml` added (packages `phantombuster`/`cqc`/`resolver`; `requests`
core, Flask/SQLAlchemy a `[webapp]` extra) and the resolver consolidated into one
`resolver/` package (canonical richer phantom shipped as package data; both
managed `launch_resolution` + new ephemeral `resolve_ephemeral` paths). See plan
PWS0 for the full verified exit.

**PWS1 is DONE** (2026-07-03, commit `a28cedb` on `main`): pinned git dep added
(`phantombuster-lib @ git+…@2189f627`), our stdlib `phantombuster.py` +
`test_phantombuster.py` deleted. The ingest contract (`ScrapedProfile` +
`parse_profile`/`parse_profiles`) moved to **new `linkedin_profiles.py`**
(transport-free; PWS3 reuses it); `enrich_linkedin.run_identification_phantom`
reworked onto the lib's `Phantombuster`. Offline suite green without our client.
See plan PWS1.

**Next session should pick up:** **PWS2** in
[`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md#pws2--resolver-provider--numeric-companyid)
— adopt the lib's **`cqc` Syndication client** for
`brandName`/`brandId`/`companiesHouseNumber` (our bulk-CSV `Provider` doesn't
store `brandName`; needs `CQC_SUBSCRIPTION_KEY`), run the lib resolver
(`from resolver import search_term, launch_resolution`/`resolve_ephemeral`) to get
the numeric LinkedIn id, **verify** the match against a CQC signal
(website/town/CH number) before trusting it, and store **`Provider.linkedin_company_id`**
(additive schema, ADR 0002; cache by `brandId` — one brand → many providers).
Then PWS3 (consume rows → `Person`/`Role`, reusing `linkedin_profiles.parse_profiles`).

**Verification command:**

```sh
# Throwaway venv (no .venv in repo; the /tmp one from this session may be gone).
# Pulls phantombuster-lib from the pinned git commit — needs network:
uv venv /tmp/cqc-venv --python 3.12 && uv pip install --python /tmp/cqc-venv/bin/python -r requirements.txt
PY=/tmp/cqc-venv/bin/python
# Our stdlib client is gone; `from phantombuster import ...` must resolve to the lib:
"$PY" -c "from phantombuster import Phantombuster; from cqc import CQC; from resolver import search_term; print('lib OK')"
# Offline suite green without our client (test_phantombuster removed in PWS1):
for t in test_cqc_mapping test_cqc_refresh test_apply_events test_companies_house test_enrich_people test_statistics test_secrets_box test_enrich_linkedin; do "$PY" $t.py >/dev/null && echo "$t OK"; done
# Pivot docs present:
grep -l "Amendment (2026-07-02)" docs/adr/0016-linkedin-phantombuster-ingestion.md
```

## Done this session

| Commit | Subject |
|---|---|
| `10156ef` | ADR 0016 — LinkedIn identification via Phantombuster |
| `fb70f0d` | LinkedIn ingestion offline mechanism (WS1–3): User/PhantomRun/secrets_box, phantombuster.py, enrich_linkedin |
| `cf504c2` | ADR 0017 — GDPR controller posture |
| `8ae5c4f` / `bba3ef5` | reconcile phantombuster.py with the real v2 API + spike (docs/spikes/phantombuster-api.md) |
| `a543a67` | launch_agent saved-config + bonusArgument |
| `d6de91c` | parse_profile maps the Employees Export `job` field (confirmed live) |
| `ddb00dc` / `dd3e7ba` | **pivot** ADR 0016 → phantombuster-lib; plan rewritten (PWS0–PWS5) |
| PR #1 (phantombuster-lib) | **security:** removed committed live API keys from the public repo |
| **PR #2 (phantombuster-lib)** | **PWS0:** `pyproject.toml` + resolver consolidated into one `resolver/` package (merged `2189f627`) |
| **`a28cedb`** | **PWS1:** depend on phantombuster-lib (pinned), retire our `phantombuster.py`; ingest contract → new `linkedin_profiles.py`; live driver → lib client |

(Earlier in the session, all on `main`: resolved the data-freshness handoff, accepted ADR 0013/0014/0015, fixed stale ADR numbering, drafted ADR 0016.)

## Open follow-ups (priority-ordered)

### 1. ✅ PWS0 — package phantombuster-lib + consolidate the resolver — DONE (2026-07-03)

Merged as phantombuster-lib PR #2 (`2189f627`). See plan PWS0 for the verified
exit. Pin PWS1's dependency to that commit.

### 2. PWS2–PWS3 — integrate into cqc-companies (L) — **PWS2 is next**

**PWS1 done** (commit `a28cedb`): the lib is a pinned dependency, our client is
retired, the ingest contract lives in `linkedin_profiles.py`, the live driver runs
on the lib. Remaining:
- **PWS2 (next):** adopt the lib's `cqc` Syndication client for `brandName` (needs
  `CQC_SUBSCRIPTION_KEY`); run the lib resolver → verify the match against a CQC
  signal → store additive **`Provider.linkedin_company_id`** (cache by `brandId`).
- **PWS3:** rework `enrich_linkedin` into a consumer of the lib's `RunResult.result`
  rows → `Person`/`Role` (ADR 0014 correlation, **reusing
  `linkedin_profiles.parse_profiles`** — the name/`job`/`profileUrl` mapping is
  already extracted). `PhantomRun` demoted to optional audit (PWS4).

### 3. PWS5 / ADR 0017 WS6 — durable erasure (M) — GATES real persistence

`SuppressedContact` tombstone + suppression check on ingest, before any real scraped person is written. See ADR 0017 §5.

### 4. External: Andrew rotates the exposed keys (not us)

phantombuster-lib's committed keys are removed from HEAD but remain in git history → **must be rotated** by the owner. Not blocking PWS2/PWS3 (offline work).

## Critical context

- **The pipeline is proven live.** URL Finder → Google-Sheet feed (via the `gws-cli` Google Workspace skill) → Company Employees Export → **23 real employee rows** scraped. But this store-phantom path is **being retired** in favour of the lib's cleaner approach (custom UK-HQ resolver phantom keyed on CQC `brandName` → numeric `companyId` → Search Export → `resultObject`, inline argument, no feed).
- **Confirmed employee field names** (locked in `parse_profile` + the spike): `profileUrl`, `name`, `firstName`, `lastName`, **`job`** (the title, e.g. "Nursing Manager at Care UK"), `location`, `query`. **No `companyName`** — company is in `job` and, authoritatively, in **`query`** (the input company URL). So an Employees Export run is **multi-company**: map each row → provider via `query`.
- **Phantombuster account state:** agents exist — URL Finder `7669492412108381`, cqc Employees Export `6041032174032850` (a duplicate + a stray gist were cleaned up). A shared Drive **Sheet** feed exists (`1P7lON09neNgdK004v6q2faW9Fmy9cQcAqVNWuKnqB2A`). Our session PB key (`.env.local`) is **not** the exposed one — safe.
- **Verified API facts** (spike `docs/spikes/phantombuster-api.md`): results at S3 `result.json` OR `containers/fetch-result-object`; **no API file-upload** (feed = a public URL: Google Sheet works, a Drive *file* URL doesn't); `launch-sync` streams, doesn't return rows; store phantoms can't be created via API (only org-owned custom scripts) — which is why the lib uses **custom** phantoms.
- **`phantombuster-lib` shape (post-PWS0):** three installable packages —
  `phantombuster` (client: `launch`, `run_and_wait`, `run_ephemeral`, `get_result`,
  `redact`), `cqc` (Syndication client), and **`resolver`** (the UK-HQ geo facet +
  `brandName` + numeric-id scrape, phantom shipped as package data; exposes
  `search_term`, `gather_cqc`, `launch_resolution` [managed] and `resolve_ephemeral`
  [one-shot]). It uses `requests`, ms timestamps, and injects the `li_at` cookie via
  the argument. On the cqc-companies side, our row→identity mapping lives in
  `linkedin_profiles.py` (`ScrapedProfile` + `parse_profile`/`parse_profiles`).
- **No `.venv`**; build a throwaway venv (verification command). The
  `/tmp/claude/pblib-work` clone **survived** the 2026-07-02→03 sessions (it's on
  `main` at the merged PWS0 commit); if it's gone next session, re-clone
  `mooperd/phantombuster-lib`. After PWS0, cqc-companies depends on the lib via a
  pinned git URL — you no longer need the clone to *use* it, only to co-maintain it.

## References

- [`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md) — PWS0–PWS5 (pivot) + historical WS1–6
- [`docs/adr/0016-linkedin-phantombuster-ingestion.md`](../adr/0016-linkedin-phantombuster-ingestion.md) — the decision + 2026-07-02 amendment
- [`docs/adr/0017-gdpr-controller-posture.md`](../adr/0017-gdpr-controller-posture.md) — erasure gate
- [`docs/spikes/phantombuster-api.md`](../spikes/phantombuster-api.md) — verified API facts + confirmed field names
- [`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib) — the acquisition layer

## Migration note

When PWS0–PWS5 land: their detail is already in `linkedin-ingestion.md` — update those PWS statuses, move ADR 0016 (and 0017 once erasure ships) Proposed→Accepted, then `git rm docs/handoffs/linkedin-ingestion-handoff.md` in the same commit as the migration.
