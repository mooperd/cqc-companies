"""Preflight check for the Phantombuster LinkedIn-ingestion config (ADR 0016).

Proves, end to end, that a live identification run *could* work before you spend
any credits: the secrets key is configured, the Phantombuster API key
authenticates, the Search Export agent exists, the resolver's cookie-source agent
exists **and has a connected LinkedIn identity** to lend, and the DB schema carries
the PWS columns. Prints a checklist; exits non-zero if any required check fails.

Read-only: it lists/fetches agents and inspects the DB, but launches nothing and
spends no credits.

    uv run python pb_doctor.py

Config it reads (from .env / .env.local):
  APP_SECRETS_KEY          — Fernet key for at-rest secret encryption
  PHANTOMBUSTER_API_KEY    — account key that authenticates our API calls
  PB_SEARCH_EXPORT_AGENT   — the saved LinkedIn Search Export agent (people scrape)
  PB_SOURCE_AGENT          — the agent whose connected LinkedIn identity the
                             resolver borrows a sessionCookie from
  DATABASE_URL             — the DB to check the schema of
"""

from __future__ import annotations

import logging
import os
import sys
from dataclasses import dataclass

from sqlalchemy import create_engine

logger = logging.getLogger("pb_doctor")

DEFAULT_DATABASE_URL = "postgresql://darwinist:darwinist@localhost:5432/darwinist"


@dataclass
class Check:
    """One preflight result: `ok` passes, `not ok` blocks a live run."""
    name: str
    ok: bool
    detail: str

    @property
    def fatal(self) -> bool:
        return not self.ok


# --- Individual checks (each pure over its injected client/engine, so testable) --


def check_secrets_key() -> Check:
    """APP_SECRETS_KEY is set and usable — secrets_box round-trips a value."""
    if not os.environ.get("APP_SECRETS_KEY"):
        return Check("secrets key", False,
                     "APP_SECRETS_KEY unset — generate one: python -m secrets_box keygen")
    import secrets_box
    try:
        if secrets_box.decrypt(secrets_box.encrypt("preflight")) != "preflight":
            return Check("secrets key", False, "secrets_box round-trip mismatch")
    except Exception as e:  # noqa: BLE001 — surface any key/format problem as a failure
        return Check("secrets key", False, f"secrets_box unusable: {e}")
    return Check("secrets key", True, "APP_SECRETS_KEY configured, round-trip OK")


def check_api_auth(pb) -> Check:
    """The Phantombuster API key authenticates — list_agents() succeeds."""
    try:
        agents = pb.list_agents()
    except Exception as e:  # noqa: BLE001
        return Check("api auth", False, f"list_agents failed (bad key?): {e}")
    return Check("api auth", True, f"authenticated — {len(agents)} agent(s) in the account")


def check_agent_exists(pb, agent_id: str | None, *, label: str, env: str) -> Check:
    """A required agent id is set and resolves to a real agent in the account."""
    if not agent_id:
        return Check(label, False, f"{env} unset")
    try:
        agent = pb.get_agent(agent_id)
    except Exception as e:  # noqa: BLE001
        return Check(label, False, f"{env}={agent_id} not found in the account: {e}")
    return Check(label, True, f"{env}={agent_id} → “{agent.get('name', '?')}”")


def check_source_cookie(pb, source_agent_id: str | None) -> Check:
    """The resolver's cookie source has a connected LinkedIn identity to lend —
    argument.identities[0].sessionCookie is present (mirrors the lib's
    resolver.linkedin_session_cookie). A LINKEDIN_SESSION_COOKIE env var overrides
    the borrow, so it satisfies this too."""
    if os.environ.get("LINKEDIN_SESSION_COOKIE"):
        return Check("linkedin session", True,
                     "LINKEDIN_SESSION_COOKIE set — overrides the agent borrow")
    if not source_agent_id:
        return Check("linkedin session", False,
                     "PB_SOURCE_AGENT unset and no LINKEDIN_SESSION_COOKIE — the "
                     "resolver has no identity to run as")
    try:
        agent = pb.get_agent(source_agent_id)
    except Exception as e:  # noqa: BLE001
        return Check("linkedin session", False,
                     f"PB_SOURCE_AGENT={source_agent_id} not found: {e}")
    from phantombuster import parse_json_field  # tolerates the JSON-string variant
    arg = parse_json_field(agent.get("argument"))
    identities = (arg.get("identities") if isinstance(arg, dict) else None) or []
    cookie = identities[0].get("sessionCookie") if identities else None
    if not cookie:
        return Check("linkedin session", False,
                     f"agent {source_agent_id} has no connected LinkedIn identity "
                     "(argument.identities[0].sessionCookie) — connect an account to it "
                     "in the Phantombuster UI")
    return Check("linkedin session", True,
                 f"agent {source_agent_id} has a connected LinkedIn identity to lend")


def check_schema(engine) -> Check:
    """The DB schema is synced to model.py — init_db has no pending DDL."""
    from init_db import plan
    pending = [s for s in plan(engine) if not s.startswith("--")]
    created = [s for s in plan(engine) if s.startswith("--")]
    gaps = pending + created
    if gaps:
        return Check("db schema", False,
                     f"{len(gaps)} pending change(s) — run: uv run python init_db.py")
    return Check("db schema", True, "schema matches model.py")


# --- Orchestration ---------------------------------------------------------------


def run_checks() -> list[Check]:
    """Run every check against the configured environment, returning the results in
    display order. Live checks are skipped (as fatals) if there's no API key."""
    checks = [check_secrets_key()]

    engine = create_engine(os.getenv("DATABASE_URL", DEFAULT_DATABASE_URL))
    checks.append(check_schema(engine))

    api_key = os.environ.get("PHANTOMBUSTER_API_KEY")
    if not api_key:
        checks.append(Check("api auth", False,
                            "PHANTOMBUSTER_API_KEY unset — cannot run the live checks"))
        return checks

    from phantombuster import Phantombuster
    pb = Phantombuster(api_key)
    checks.append(check_api_auth(pb))
    checks.append(check_agent_exists(
        pb, os.environ.get("PB_SEARCH_EXPORT_AGENT"),
        label="search export agent", env="PB_SEARCH_EXPORT_AGENT"))
    checks.append(check_source_cookie(pb, os.environ.get("PB_SOURCE_AGENT")))
    return checks


def report(checks: list[Check]) -> int:
    """Print the checklist; return a process exit code (non-zero if any fatal)."""
    for c in checks:
        print(f"  [{'✓' if c.ok else '✗'}] {c.name}: {c.detail}")
    fatals = [c for c in checks if c.fatal]
    print()
    if fatals:
        print(f"FAIL — {len(fatals)} blocking issue(s); a live run would not work yet.")
        return 1
    print("OK — every check passed; the Phantombuster config is ready for a live run.")
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    from dotenv import load_dotenv

    load_dotenv()
    load_dotenv(".env.local", override=True)

    print("Phantombuster preflight (ADR 0016) — read-only, spends no credits\n")
    return report(run_checks())


if __name__ == "__main__":
    sys.exit(main())
