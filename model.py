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
    people = db.relationship('Person', backref='provider', lazy=True)

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

# A decision-maker at a provider organisation — the CRM target. Replaces the
# old flat `Contact` placeholder (deleted in ADR 0012). Seeded from Companies
# House directors (ADR 0013) and, later, LinkedIn; manual entry always works.
# Interaction + User (the other two Phase-1 entities) are decided in ADR 0012
# but not yet implemented — see docs/plans/crm-phase1.md.
class Person(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(255), nullable=False, index=True)
    role = db.Column(db.String(255))
    # Provenance: which source asserted this person, and how much we trust it.
    # The source-hierarchy / conflict rule lives in ADR 0013 §3.
    source = db.Column(db.String(50), nullable=False)  # companies_house | phantombuster:<phantom> | manual
    confidence = db.Column(db.String(20))  # high | medium | low
    # Director appointment dates from Companies House (resignation NULL =
    # currently active). Real db.Date, not String like the CSV-derived date
    # fields: these arrive as ISO dates from the CH API, and the ADR 0013 merge
    # rule queries role-currency (resignation_date IS NULL).
    appointment_date = db.Column(db.Date)
    resignation_date = db.Column(db.Date)
    provider_id = db.Column(db.Integer, db.ForeignKey('provider.id'), nullable=False, index=True)
