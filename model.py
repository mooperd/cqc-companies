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
