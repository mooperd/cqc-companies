from flask_sqlalchemy import SQLAlchemy

db = SQLAlchemy()

class Provider(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    cqc_provider_id = db.Column(db.String(100), index=True)
    # Carried through from the CQC HSCA export (Locations.csv column
    # "Provider Companies House Number"); the seed for future Companies
    # House director enrichment — see docs/adr/0013-companies-house-source.md
    # and docs/plans/companies-house-enrichment.md. Nullable: not every
    # provider is a registered company (sole traders, NHS bodies, etc.).
    companies_house_number = db.Column(db.String(20), index=True)
    website = db.Column(db.String(500))
    email_address = db.Column(db.String(255))
    phone_number = db.Column(db.String(50))
    address_1 = db.Column(db.String(255))
    address_2 = db.Column(db.String(255))
    town_city = db.Column(db.String(255))
    county = db.Column(db.String(255))
    postcode = db.Column(db.String(20))

    # Lifecycle + freshness (ADR 0015). active=False is a soft-delete for a
    # provider dropped from the CQC export (rows retained, queries filter on it).
    active = db.Column(db.Boolean, nullable=False, default=True)
    removed_at = db.Column(db.DateTime)
    # Companies House enrichment freshness: when we last successfully polled, and
    # the watermark (latest officer/PSC filing seen) for the filing-history check.
    ch_enriched_at = db.Column(db.DateTime)
    ch_filing_watermark = db.Column(db.String(50))

    facilities = db.relationship('Facility', backref='provider', lazy=True)
    roles = db.relationship('Role', backref='provider', lazy=True)

class Facility(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    address_1 = db.Column(db.String(255))
    address_2 = db.Column(db.String(255))
    town_city = db.Column(db.String(255))
    county = db.Column(db.String(255))
    postcode = db.Column(db.String(20))
    phone_number = db.Column(db.String(50))
    cqc_location_id = db.Column(db.String(100), index=True)
    website = db.Column(db.String(500))
    local_authority = db.Column(db.String(255))
    region = db.Column(db.String(255))
    report_publication_date = db.Column(db.String(50))
    url = db.Column(db.String(500))
    also_known_as = db.Column(db.Text)
    specialisms_services = db.Column(db.Text)
    service_types = db.Column(db.Text)
    email_address = db.Column(db.String(255))
    
    # Location enrichment fields
    registered_manager = db.Column(db.String(255))
    location_uprn = db.Column(db.String(50))
    location_telephone = db.Column(db.String(50))
    location_web_address = db.Column(db.String(500))
    primary_inspection_category = db.Column(db.String(255))
    care_home_beds = db.Column(db.Integer)
    location_start_date = db.Column(db.String(50))
    location_end_date = db.Column(db.String(50))
    dormant = db.Column(db.String(10))
    latest_overall_rating = db.Column(db.String(50))
    publication_date = db.Column(db.String(50))
    service_users_supported = db.Column(db.Text)
    care_home_size_band = db.Column(db.String(100))
    location_length_service_band = db.Column(db.String(100))
    safe_rating = db.Column(db.String(50))
    effective_rating = db.Column(db.String(50))
    caring_rating = db.Column(db.String(50))
    responsive_rating = db.Column(db.String(50))
    well_led_rating = db.Column(db.String(50))

    # Lifecycle (ADR 0015). CQC removals are per-location, so soft-delete lives
    # here; Provider.active is derived as "has any active facility".
    active = db.Column(db.Boolean, nullable=False, default=True)
    removed_at = db.Column(db.DateTime)

    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), nullable=False, index=True)

# A correlated human — one record per person across Companies House's two
# endpoints (officers + PSC) and across the companies they're tied to (ADR 0014,
# which reshapes the flat Person from ADR 0012). The link to a provider lives on
# Role, so one Person can hold many Roles. Seeded from Companies House; later
# from LinkedIn and manual entry.
class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)  # display form
    # Parsed identity used for correlation. The DOB (month+year, all CH gives for
    # individuals) is the anchor; surname + first forename disambiguate. See the
    # correlation rules in ADR 0014.
    surname = db.Column(db.String(255), index=True)
    forenames = db.Column(db.String(255))
    normalized_name = db.Column(db.String(255), index=True)
    dob_year = db.Column(db.Integer, index=True)
    dob_month = db.Column(db.Integer)
    nationality = db.Column(db.String(100))
    # LinkedIn identity + dedup key (ADR 0016). A person has one LinkedIn profile;
    # the URL is the stable key for re-scrapes and is stronger than name. Null for
    # people who only ever came from Companies House or manual entry.
    linkedin_url = db.Column(db.String(500), index=True)
    # How sure we are this is one real human: 'low' when created without a DOB
    # anchor (not auto-merged). See ADR 0014.
    match_confidence = db.Column(db.String(20))  # high | low

    roles = db.relationship('Role', backref='person', lazy=True)


# One source-fact tying a Person to a Provider (ADR 0014): a directorship, a
# person-with-significant-control relationship, a manual note, etc. The ADR 0013
# §3 source-hierarchy (manual > Companies House for the same person+provider)
# applies at this level.
class Role(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    person_id = db.Column(db.Integer, db.ForeignKey('person.id'), nullable=False, index=True)
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), nullable=False, index=True)
    role_type = db.Column(db.String(50), nullable=False)  # director | psc | manual | ...
    source = db.Column(db.String(50), nullable=False)  # companies_house:officers | companies_house:psc | manual | phantombuster:<phantom>
    confidence = db.Column(db.String(20))  # high | medium | low
    # appointed_on / notified_on, and resigned_on / ceased_on (NULL end = active).
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    # PSC natures_of_control summary (ownership %, voting rights); null for officers.
    control_nature = db.Column(db.Text)


# A queryable materialization of the git-committed change-event files (ADR 0015).
# The files under data/changes/ are canonical; this table is a projection rebuilt
# by replaying them, and the substrate outreach Tasks key off (Phase 4).
# Immutable: an observation, not a task.
class ChangeEvent(db.Model):
    __tablename__ = 'change_events'
    id = db.Column(db.Integer, primary_key=True)
    observed_at = db.Column(db.DateTime)   # when we detected it (the file's run)
    effective_date = db.Column(db.Date)    # source date of the change, if known
    source = db.Column(db.String(50), nullable=False)  # cqc | companies_house:officers | companies_house:psc
    change_type = db.Column(db.String(50), nullable=False)  # provider_added|removed|updated | location_* | role_appointed|ended|changed
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), nullable=False, index=True)
    person_id = db.Column(db.Integer, db.ForeignKey('person.id'), index=True)
    role_id = db.Column(db.Integer, db.ForeignKey('role.id'))
    details = db.Column(db.JSON)  # field-level changes / role info


# Ledger of which change-event files have been applied to this DB projection
# (ADR 0015), so apply-latest is idempotent and resumable.
class AppliedEventFile(db.Model):
    __tablename__ = 'applied_event_file'
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(255), nullable=False, unique=True, index=True)
    applied_at = db.Column(db.DateTime)


class _EncryptedField:
    """Descriptor for an at-rest-encrypted secret. Reads/writes PLAINTEXT through
    secrets_box; the ciphertext lives in the sibling `<name>_enc` Column. There is
    no plaintext column, so a credential can't be persisted unencrypted by
    accident (fail-closed without APP_SECRETS_KEY). Lazy-imports secrets_box in
    one place so `import model` stays light (smoke check) — see ADR 0016."""

    def __set_name__(self, owner, name: str) -> None:
        self._col = f"{name}_enc"

    def __get__(self, obj, objtype=None):
        if obj is None:
            return self  # class-level access (SQLAlchemy declarative scan)
        import secrets_box
        token = getattr(obj, self._col)
        return secrets_box.decrypt(token) if token else None

    def __set__(self, obj, value: str | None) -> None:
        import secrets_box
        setattr(obj, self._col, secrets_box.encrypt(value) if value else None)


# A team member doing outreach (ADR 0012, built minimally for ADR 0016). Acts
# under their own LinkedIn identity, so phantoms run with THIS user's session +
# Phantombuster key — both held encrypted at rest (the *_enc columns are
# ciphertext; assign/read the plaintext via the _EncryptedField descriptors).
# Table is `app_user` because `user` is reserved in PostgreSQL.
class User(db.Model):
    __tablename__ = 'app_user'
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255))
    email = db.Column(db.String(255), unique=True, index=True)
    # Ciphertext at rest; access the plaintext via the descriptors below.
    linkedin_session_cookie_enc = db.Column(db.Text)
    phantombuster_api_key_enc = db.Column(db.Text)
    # `user.linkedin_session_cookie = "..."` encrypts into linkedin_session_cookie_enc;
    # reading it decrypts. Same for the Phantombuster key.
    linkedin_session_cookie = _EncryptedField()
    phantombuster_api_key = _EncryptedField()

    phantom_runs = db.relationship('PhantomRun', backref='user', lazy=True)


# One Phantombuster run (ADR 0016): an async LinkedIn scrape (or, later, an
# action) launched under a User's credentials, against an optional target
# provider. The durable record of a scrape — status drives resume, credits_spent
# meters cost. identification phantoms feed Person/Role via enrich_linkedin.
class PhantomRun(db.Model):
    __tablename__ = 'phantom_run'
    id = db.Column(db.Integer, primary_key=True)
    phantom = db.Column(db.String(100), nullable=False)  # e.g. company-people-scraper
    user_id = db.Column(db.Integer, db.ForeignKey('app_user.id'), nullable=False, index=True)
    # Target org for company-scoped phantoms; null for a search not tied to one.
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), index=True)
    input = db.Column(db.JSON)  # the launch arguments
    status = db.Column(db.String(20), nullable=False, default='queued')  # queued|launched|running|finished|failed
    launched_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    output_ref = db.Column(db.String(500))  # where the fetched result is stored
    credits_spent = db.Column(db.Integer)
    error = db.Column(db.Text)
