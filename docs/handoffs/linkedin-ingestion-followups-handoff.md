# Handoff — LinkedIn ingestion is operational; distribution + batch-scale remain

**Created:** 2026-07-07
**Working tree:** clean
**Branch:** main (all pushed, CI green)

<!--
Reminder: a handoff is ephemeral. At resolution, every load-bearing piece below
migrates into a docs/adr/, docs/plans/, docs/spikes/, or design-doc home, and this
file is then `git rm`'d in the same commit as the migration.
See docs/handoff-discipline.md for the migration table.
-->

## Goal & next-up

**Goal of this session:** take the fixture-tested LinkedIn ingestion (ADR 0016) to a
real live run, and settle the acquisition architecture. Both achieved: a gated live
scrape returned **10 real Barchester people** (Exp 2), and the resolver was moved off
the flaky Phantombuster phantom to a **deterministic no-auth public-page fetch** (PWS2).

**Next session should pick up:** the **scraped-data-distribution decision** — the one
thing genuinely blocked on a human call. See
[`docs/spikes/scraped-data-distribution.md`](../spikes/scraped-data-distribution.md)
§"Open decisions": choose **B (encrypted-in-git)** vs **C (out-of-band store)** for
distributing scraped Person/Role data as ADR-0015-style change-sets, then write the
ADR and build the change-set emit/apply.

**Verification command:**

```sh
# 1. Full offline suite (14 files, self-contained sqlite) — all pass:
for t in test_*.py; do uv run python "$t" >/dev/null 2>&1 && echo "PASS $t" || echo "FAIL $t"; done

# 2. No-auth resolver works live (plain public GET; no keys, no DB writes needed):
python -c "from resolve_company_id import public_resolver as r; print(r()('HC-One Limited'))"
# Expect: {'companyId': '2851202', 'name': 'HC-One', ...}

# 3. (Optional, needs .env.local + a fresh Phantombuster session) preflight the scrape:
uv run python pb_doctor.py     # Expect: every check ✓
```

## Done this session

| Commit | Subject | Notes |
|---|---|---|
| `a33a3b6` | idempotent schema sync (`init_db.py`) | create-or-update; adds missing columns |
| `4ab34a2` | drop the per-user LinkedIn cookie | it's Phantombuster's session, not ours |
| `2bdf3b1` | Search Export key is `linkedInSearchUrl` via `bonus_argument` | was `search`; found via live config |
| `8aaa9a2` | `pb_doctor` preflight + `run_extraction` driver | doctor caught a wrong agent id immediately |
| `9529fe9` | load `.env` before importing the resolver; `--keywords` | import-order bug |
| `089d706` `97f988f` | acquisition spike (+ correction) | the load-bearing design record |
| `15924a7` | gate phantom success on `exitCode==0` | not `lastEndStatus` (None on success) |
| `507202b` | Exp 2 success — 10 real Barchester people | store Search Export works end to end |
| `1f882cb` | gitignore DB dumps (contain PII) | |
| `77dc4ed` | provider detail page `/provider/<id>` | first UI for person/role data |
| `ce7e591` `2ef3e3f` | README + dump restore + distribution spike | reproducibility boundaries |
| `a306500` | **PWS2 resolver → no-auth public-page fetch** | `linkedin_public.py`; deterministic |

## Open follow-ups (priority-ordered)

### 1. Scraped-data distribution decision → ADR → build (medium–large)

The blocker. LinkedIn people exist only in a `pg_dump` (not reproducible). The spike
[`scraped-data-distribution.md`](../spikes/scraped-data-distribution.md) frames it:
reuse ADR 0015's change-set *mechanism*, but personal-data change-sets can't be
plaintext-in-git (GDPR erasure vs immutable history). Decide B (encrypted-in-git,
reuse `secrets_box`) vs C (mutable access-controlled store). Then: write the ADR
(extends 0015, amends 0017) and build a scraped-Person/Role change-set emit +
`apply_events`-style replay (must consult `SuppressedContact` on apply).

### 2. ADR 0017 legal sign-off gates *production* live scraping (external)

The erasure/suppression **mechanism has shipped** (`suppression.py`); what remains is
the legal precondition in [ADR 0017](../adr/0017-gdpr-controller-posture.md) — a
reviewed LIA, privacy-notice wording, ICO registration. Not code. The one gated live
run (Exp 2, `--limit 10`) was a controlled test; ongoing/at-scale scraping should wait
on this. The plan's last box ([linkedin-ingestion.md](../plans/linkedin-ingestion.md)
"Live run") is effectively met for a test; keep it open until legal clears production.

### 3. Resolver batch-scale polish (small–medium)

`resolve_all` is wired to `public_resolver()` but **never run over many providers**. For
batch scale (deferred, noted in the spike's Findings/Decision):
- `linkedin_public._fetch` — add `Accept-Encoding: gzip` + connection reuse (one pass).
- A **golden real-page fixture** so LinkedIn markup drift fails CI loudly (today the
  regex fails *closed* → silent resolution-rate drop). Tests use synthetic HTML only.
- Decide the deferred `verify_match` question: should a website domain **mismatch** be
  non-fatal (corroborating) rather than a hard reject? The public resolver currently
  sidesteps it by omitting website entirely.

### 4. `pb_doctor` could check session *freshness* (small)

The doctor checks the cookie **exists**, not that it's **valid** — a stale-but-present
cookie passes the doctor yet fails the scrape with `exit 84`. It could inspect the
agent's last container for `exit 84` and warn. Would have saved an hour today.

## Critical context

- **Phantombuster's LinkedIn session EXPIRES and is the #1 gotcha.** Every failure this
  session (empty resolver, `exit 84` scrape) traced to one stale session. Reconnecting
  centrally is **not enough** — it must write through to *the agent's own identity*; the
  signal is the agent's cookie **fingerprint changing** (read it via `pb.get_agent`).
  We only got a green scrape once the fingerprint moved `…1ktbnK` → `…y0omUN`.
- **Two ids, easily confused:** `PB_SEARCH_EXPORT_AGENT=8897510742354773` is the *agent*;
  `7894780824982741` (in the agent's saved argument) is the *LinkedIn identity*. Only the
  agent id is ours to configure.
- **The `.venv` phantombuster-lib `resolver_phantom.js` has a local, uncommitted** About-page
  fix (fall back to base URL on `ERR_ABORTED`) from probing Option 1. It is **not persisted
  anywhere real** — if the phantom path is ever revived, that fix belongs in the
  `mooperd/phantombuster-lib` repo, not `.venv`.
- `run.sh` writes a **wrong** `DATABASE_URL` (`postgres:password@…/crm_db`) inconsistent
  with `app.py`'s `darwinist` default — flagged in the README, not fixed.

## References

- [`docs/spikes/linkedin-acquisition-approach.md`](../spikes/linkedin-acquisition-approach.md) — the resolver/scrape decision (Resolved, with correction)
- [`docs/spikes/scraped-data-distribution.md`](../spikes/scraped-data-distribution.md) — follow-up 1's design frame (Open)
- [`docs/plans/linkedin-ingestion.md`](../plans/linkedin-ingestion.md) — PWS state (PWS2 amended 2026-07-07)
- [`docs/adr/0016-linkedin-phantombuster-ingestion.md`](../adr/0016-linkedin-phantombuster-ingestion.md) — three amendments (07-02, 07-05, 07-07)
- [`docs/adr/0017-gdpr-controller-posture.md`](../adr/0017-gdpr-controller-posture.md) — the legal gate (follow-up 2)

## Migration note

When these resolve:
- Follow-up 1 → a new ADR (extends 0015 / amends 0017) + plan workstream; the spike marks Resolved.
- Follow-up 2 → when legal clears, tick the plan's "Live run" box and note it in the plan.
- Follow-up 3 → fold into the plan's PWS2 (batch-scale notes) + code comments in `linkedin_public.py`.
- Follow-up 4 → a `pb_doctor.py` check + a line in its docstring.
- Critical-context bullets (session expiry, the two ids) → already in the acquisition spike; the
  `run.sh` and `.venv` bullets → a code comment / a quick fix, then drop.
- Then `git rm docs/handoffs/linkedin-ingestion-followups-handoff.md` in the migration commit.
