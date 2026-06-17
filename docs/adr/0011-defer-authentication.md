# ADR 0011 — Defer authentication; remove the unwired `google_login.py`

**Status:** Accepted (2026-06-17).

**TL;DR.** In the context of a public, read-only CQC browser with no per-user
state, facing an orphaned Google-OAuth module left over from an earlier
attempt, we chose to **delete `google_login.py` and defer authentication
entirely** to achieve a smaller, honest codebase, accepting that auth will be
rebuilt from scratch — behind its own plan and ADR — when a real driver
appears.

## Context

`google_login.py` (≈4 KB) implemented a Google OAuth2 sign-in flow
(`OAuth2Session`, reading `GOOGLE_OAUTH2_CONFIG_B64`). It was aspirational scope
from a prior attempt and was never finished:

- It is **imported from nowhere** — `app.py` does not reference it.
- It **cannot even import**: line 6 is `from app.telemetry import …`, but no
  `app/telemetry.py` exists; and it imports `requests_oauthlib`, which is not in
  `requirements.txt`.

So it was dead, non-functional code that *looked* like a feature — a trap for
any future contributor (or Claude session) who might try to use or "fix" it.
The app today has no per-user state, no sessions in use, and no feature that
needs a logged-in user. See
[`docs/plans/initial-debt-and-questions.md`](../plans/initial-debt-and-questions.md)
WS5, which framed this as a fork in the road.

## Decision

1. **Delete `google_login.py`.** It carries no working behaviour to preserve.
2. **Defer authentication** as a feature until a concrete driver exists (the
   CRM phase of [`docs/product-vision.md`](../product-vision.md) is the likely
   one — recording who logged an interaction needs a user identity).
3. When auth is genuinely needed, **start fresh**: open a `docs/plans/auth.md`
   scoping the work and a successor ADR recording the mechanism (provider,
   what gets gated, session/user model). Do not resurrect this file.

## Alternatives considered

- **Wire `google_login.py` up (Path B).** Rejected: it is a whole project, not
  a debt fix — it would require building a telemetry layer (`app/telemetry.py`),
  blueprinting the login routes into `app.py`, adding `requests_oauthlib`,
  choosing a user model, and deciding what to gate. There is no current feature
  demanding any of that, so committing now would be speculative.
- **Leave the file in place, unwired.** Rejected: broken dead code that can't
  import is a liability — it misleads readers and rots silently. The history is
  preserved in git if it is ever wanted as a reference.

## Consequences

- The repo no longer ships a non-functional OAuth module; the import surface is
  honest (the CI smoke check imports only the four live entry points anyway).
- No authentication exists — fine, because nothing currently needs it.
- The `SECRET_KEY` prerequisite that any auth would share is already handled:
  [ADR 0003 Amendment (2026-06-17)](0003-server-rendered-flask-jinja.md#amendment-2026-06-17--secret_key-hardcoding-resolved)
  made it env-driven and fail-loud (WS3). So a future auth effort starts from a
  clean, working secret story rather than the old placeholder.

## Walk-back options

- **If a feature needs a logged-in user** (most likely the CRM
  `Person`/`Interaction` work in the product vision) — open `docs/plans/auth.md`,
  pick an auth provider, and write the successor ADR. Build it fresh against the
  then-current `app.py`; the deleted file's approach (raw `requests_oauthlib` +
  bespoke telemetry decorators) is not a template worth reviving.

## Links

- `docs/plans/initial-debt-and-questions.md` — WS5, the decision this records.
- [ADR 0003](0003-server-rendered-flask-jinja.md) — server-rendered Flask; its
  Walk-back options anticipated auth, and its 2026-06-17 Amendment cleared the
  `SECRET_KEY` precondition.
- [ADR 0001](0001-provider-facility-domain-model.md) — the `Contact`/CRM
  direction whose future work is the likely driver for real auth.
- `docs/product-vision.md` — the CRM + outreach direction that will reopen this.
