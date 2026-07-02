# Handoff — LinkedIn ingestion (ADR 0016): pivoted to phantombuster-lib; PWS0 (package the lib) is next

**Created:** 2026-07-02
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

**Next session should pick up:** **PWS0** in
[`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md#pws0--package-phantombuster-lib-cross-repo)
— package `phantombuster-lib` (add `pyproject.toml`) and **promote/consolidate the
resolver** (it lives in `webapp/resolver.py` + `webapp/resolver_phantom.js` *and* is
re-implemented in `examples/cqc_to_linkedin.py` + `examples/linkedin_company_id.js`
— two `.js` variants) into one importable `resolver` package. We have **WRITE
access** — self-service PR we merge. Then PWS1 (add the git dep, retire our
`phantombuster.py`) → PWS2 (resolver → `Provider.linkedin_company_id`, verified) →
PWS3 (consume rows → `Person`/`Role`).

**Verification command:**

```sh
# Throwaway venv (no .venv in repo; /tmp one from this session is gone):
uv venv /tmp/cqc-venv --python 3.12 && uv pip install --python /tmp/cqc-venv/bin/python -r requirements.txt
PY=/tmp/cqc-venv/bin/python
# Offline suite green (cqc-companies side; NB phantombuster.py is slated for retirement in PWS1):
for t in test_cqc_mapping test_cqc_refresh test_apply_events test_companies_house test_enrich_people test_statistics test_secrets_box test_phantombuster test_enrich_linkedin; do "$PY" $t.py >/dev/null && echo "$t OK"; done
# Pivot docs present:
grep -l "Amendment (2026-07-02)" docs/adr/0016-linkedin-phantombuster-ingestion.md
# phantombuster-lib security fix merged:
gh pr view 1 --repo mooperd/phantombuster-lib --json state --jq .state   # MERGED
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

(Earlier in the session, all on `main`: resolved the data-freshness handoff, accepted ADR 0013/0014/0015, fixed stale ADR numbering, drafted ADR 0016.)

## Open follow-ups (priority-ordered)

### 1. PWS0 — package phantombuster-lib + consolidate the resolver (M, cross-repo)

See plan PWS0. Key facts already established:
- **WRITE access confirmed** (default branch `main`, no CLAUDE.md conventions). Work on a branch → PR → merge (rebase, per global rules).
- Add `pyproject.toml`: core dep `requests`; Flask/SQLAlchemy as a `[webapp]` extra (demo-only).
- The resolver is **duplicated** — consolidate `webapp/resolver.py` + `examples` re-impl into one `resolver` package; ship the `.js` as package data (`os.path.dirname(__file__)`); repoint `webapp` (`from webapp import resolver`) + examples.

### 2. PWS1–PWS3 — integrate into cqc-companies (L)

Add `phantombuster-lib @ git+...` (pin to a commit/tag); **retire `phantombuster.py`/`test_phantombuster.py`**; rework `enrich_linkedin` into a consumer of `RunResult.result` rows → `Person`/`Role` (ADR 0014 correlation, reusing the name/`job`/`profileUrl` mapping); add `Provider.linkedin_company_id`; adopt the lib's `cqc` Syndication client for `brandName` (needs `CQC_SUBSCRIPTION_KEY`). `PhantomRun` demoted to optional audit.

### 3. PWS5 / ADR 0017 WS6 — durable erasure (M) — GATES real persistence

`SuppressedContact` tombstone + suppression check on ingest, before any real scraped person is written. See ADR 0017 §5.

### 4. External: Andrew rotates the exposed keys (not us)

phantombuster-lib's committed keys are removed from HEAD but remain in git history → **must be rotated** by the owner. Not blocking PWS0.

## Critical context

- **The pipeline is proven live.** URL Finder → Google-Sheet feed (via the `gws-cli` Google Workspace skill) → Company Employees Export → **23 real employee rows** scraped. But this store-phantom path is **being retired** in favour of the lib's cleaner approach (custom UK-HQ resolver phantom keyed on CQC `brandName` → numeric `companyId` → Search Export → `resultObject`, inline argument, no feed).
- **Confirmed employee field names** (locked in `parse_profile` + the spike): `profileUrl`, `name`, `firstName`, `lastName`, **`job`** (the title, e.g. "Nursing Manager at Care UK"), `location`, `query`. **No `companyName`** — company is in `job` and, authoritatively, in **`query`** (the input company URL). So an Employees Export run is **multi-company**: map each row → provider via `query`.
- **Phantombuster account state:** agents exist — URL Finder `7669492412108381`, cqc Employees Export `6041032174032850` (a duplicate + a stray gist were cleaned up). A shared Drive **Sheet** feed exists (`1P7lON09neNgdK004v6q2faW9Fmy9cQcAqVNWuKnqB2A`). Our session PB key (`.env.local`) is **not** the exposed one — safe.
- **Verified API facts** (spike `docs/spikes/phantombuster-api.md`): results at S3 `result.json` OR `containers/fetch-result-object`; **no API file-upload** (feed = a public URL: Google Sheet works, a Drive *file* URL doesn't); `launch-sync` streams, doesn't return rows; store phantoms can't be created via API (only org-owned custom scripts) — which is why the lib uses **custom** phantoms.
- **`phantombuster-lib` shape:** `phantombuster/` (client: `run_and_wait`, `run_ephemeral`, `redact`) + `cqc/` (Syndication client) are the library; the valuable **resolver** (UK-HQ geo facet + `brandName` + numeric-id scrape) is in `webapp/` + `examples/` (not yet packaged). It uses `requests`, ms timestamps, and injects the `li_at` cookie via the argument.
- **No `.venv`**; build a throwaway venv (verification command). `/tmp/claude/pblib-work` clone is gone next session — re-clone.

## References

- [`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md) — PWS0–PWS5 (pivot) + historical WS1–6
- [`docs/adr/0016-linkedin-phantombuster-ingestion.md`](../adr/0016-linkedin-phantombuster-ingestion.md) — the decision + 2026-07-02 amendment
- [`docs/adr/0017-gdpr-controller-posture.md`](../adr/0017-gdpr-controller-posture.md) — erasure gate
- [`docs/spikes/phantombuster-api.md`](../spikes/phantombuster-api.md) — verified API facts + confirmed field names
- [`mooperd/phantombuster-lib`](https://github.com/mooperd/phantombuster-lib) — the acquisition layer

## Migration note

When PWS0–PWS5 land: their detail is already in `linkedin-ingestion.md` — update those PWS statuses, move ADR 0016 (and 0017 once erasure ships) Proposed→Accepted, then `git rm docs/handoffs/linkedin-ingestion-handoff.md` in the same commit as the migration.
