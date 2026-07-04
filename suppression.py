"""Durable erasure + suppression — the load-bearing GDPR mechanism (ADR 0017 §5).

An erasure/objection request deletes a `Person` (and their `Role`s) AND writes a
`SuppressedContact` tombstone: a one-way **hash** of a stable identifier
(`linkedin_url` and/or the canonical name form — ADR 0014's `normalized_name`,
*not* a claim that names can be truly normalised) — never the profile itself. The
hash is over the same canonical key the ingest path matches on, so suppression and
correlation can't disagree. Every ingest path consults the suppression list
**before** creating a `Person`, so a later scrape of the same company can't
resurrect an erased contact. Without this,
erasure is theatre (the monthly re-scrape brings them straight back).

Separately, `purge_stale` implements time-boxed retention (ADR 0017 §4): it deletes
scraped contacts with no live relationship past the window, but writes **no**
tombstone — a legitimate future scrape may re-acquire them; only an explicit
erasure suppresses.

Hashing is deliberately not reversible: the suppression list must not become a
back-door personal-data store (ADR 0017, alternatives considered).
"""

from __future__ import annotations

import datetime as dt
import hashlib

from sqlalchemy import exists

from model import Person, Role, SuppressedContact


def hash_identifier(value: str) -> str:
    """SHA-256 of an identifier — the suppression key.

    The input must already be the **canonical representation the ingest path keys
    on**: `Identity.normalized_name` (ADR 0014) for a name, the stored `linkedin_url`
    for a profile. Hashing it verbatim makes the suppression key provably identical
    to the correlation/dedup key, so "is this suppressed?" asks exactly "would ingest
    treat this as the same person?". We deliberately do NOT attempt to *normalise* a
    name here — names can't be canonicalised in general (ordering, diacritics,
    nicknames, transliteration). We only reuse the one canonical form the system has
    already committed to, with its known limits (ADR 0017 open knobs / walk-back:
    `linkedin_url` is the strong key; name-only risks over-/under-suppression)."""
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _keys(linkedin_url: str | None, normalized_name: str | None) -> list[tuple[str, str]]:
    """The (key_type, hash) pairs present for a contact."""
    keys: list[tuple[str, str]] = []
    if linkedin_url:
        keys.append(("linkedin_url", hash_identifier(linkedin_url)))
    if normalized_name:
        keys.append(("name", hash_identifier(normalized_name)))
    return keys


def is_suppressed(session, *, linkedin_url: str | None = None,
                  normalized_name: str | None = None) -> bool:
    """True if any of this contact's identifiers has an erasure tombstone."""
    hashes = [h for _, h in _keys(linkedin_url, normalized_name)]
    if not hashes:
        return False
    return session.query(SuppressedContact).filter(
        SuppressedContact.identifier_hash.in_(hashes)
    ).first() is not None


def suppress(session, *, linkedin_url: str | None = None,
             normalized_name: str | None = None, reason: str | None = None,
             now: dt.datetime | None = None) -> list[SuppressedContact]:
    """Write tombstones for a contact's identifiers (idempotent). Caller commits."""
    now = now or dt.datetime.now(dt.timezone.utc)
    created: list[SuppressedContact] = []
    for key_type, digest in _keys(linkedin_url, normalized_name):
        if session.query(SuppressedContact).filter_by(identifier_hash=digest).first():
            continue  # already suppressed — keep it idempotent
        tomb = SuppressedContact(identifier_hash=digest, key_type=key_type,
                                 reason=reason, suppressed_at=now)
        session.add(tomb)
        created.append(tomb)
    return created


def erase_person(session, person: Person, *, reason: str | None = None,
                 now: dt.datetime | None = None) -> list[SuppressedContact]:
    """Erase a person: delete their `Role`s and the `Person`, and write suppression
    tombstones so a re-scrape can't bring them back (ADR 0017 §5). Caller commits."""
    tombs = suppress(session, linkedin_url=person.linkedin_url,
                     normalized_name=person.normalized_name, reason=reason, now=now)
    session.query(Role).filter_by(person_id=person.id).delete()
    session.delete(person)
    return tombs


def purge_stale(session, *, retention_days: int = 730,
                now: dt.datetime | None = None) -> int:
    """Retention purge (ADR 0017 §4): delete low-confidence (scraped) contacts with
    no live relationship whose `acquired_at` is older than the window, returning the
    count. A `manual` role counts as a live relationship and exempts the person.
    Writes NO tombstone (a future scrape may legitimately re-acquire). Caller commits.

    NB: with no `Interaction` entity yet (ADR 0012), "live relationship" is proxied
    by match_confidence + a manual role; refine to Interaction/role-activity when it
    lands. Default window 730d = 24 months (ADR 0017 open knob)."""
    now = now or dt.datetime.now(dt.timezone.utc)
    cutoff = now - dt.timedelta(days=retention_days)
    has_manual_role = exists().where(
        (Role.person_id == Person.id) & (Role.source == "manual")
    )
    stale = session.query(Person).filter(
        Person.match_confidence == "low",
        Person.acquired_at.isnot(None),
        Person.acquired_at < cutoff,
        ~has_manual_role,
    )
    purged = 0
    for person in stale.all():
        session.query(Role).filter_by(person_id=person.id).delete()
        session.delete(person)
        purged += 1
    return purged
