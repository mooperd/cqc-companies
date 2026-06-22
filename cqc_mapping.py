"""Canonical row→model field mappings for CQC CSV data (ADR 0015).

Single source of truth for turning a directory row (output.csv shape) into
Provider/Facility fields, and a locations row (Locations.csv shape) into Facility
enrichment fields. Used by both the one-time seed importers (import_records,
enrich_locations) and the incremental delta-apply (apply_events) so the two
can't drift as CQC columns change.
"""

from __future__ import annotations


def _s(row: dict, key: str) -> str:
    return (row.get(key) or "").strip()


def parse_bed_count(value: str | None) -> int | None:
    """Bed count as int, or None when blank / non-numeric."""
    value = (value or "").strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def provider_fields(row: dict) -> dict:
    """Provider fields from a directory (output.csv) row."""
    return {
        "name": _s(row, "Provider name"),
        "cqc_provider_id": _s(row, "CQC Provider ID (for office use only)") or None,
        "website": _s(row, "Website"),
    }


def facility_fields(row: dict) -> dict:
    """Facility fields from a directory (output.csv) row (excludes provider_id)."""
    return {
        "name": _s(row, "Name"),
        "address_1": _s(row, "Address 1"),
        "address_2": _s(row, "Address 2"),
        "town_city": _s(row, "Town/City"),
        "county": _s(row, "County"),
        "postcode": _s(row, "Postcode"),
        "phone_number": _s(row, "Phone number"),
        "cqc_location_id": _s(row, "CQC Location ID (for office use only)"),
        "website": _s(row, "Website"),
        "local_authority": _s(row, "Local authority"),
        "region": _s(row, "Region"),
        "report_publication_date": _s(row, "Report publication date"),
        "url": _s(row, "URL"),
        "also_known_as": _s(row, "Also known as"),
        "specialisms_services": _s(row, "Specialisms/services"),
        "service_types": _s(row, "Service types"),
        "email_address": "",
    }


def facility_enrichment_fields(row: dict) -> dict:
    """Facility enrichment fields from a locations (Locations.csv) row."""
    return {
        "registered_manager": _s(row, "Registered manager"),
        "location_uprn": _s(row, "Location UPRN"),
        "location_telephone": _s(row, "Location telephone number"),
        "location_web_address": _s(row, "Location Web Address"),
        "primary_inspection_category": _s(row, "Primary inspection category"),
        "care_home_beds": parse_bed_count(row.get("Care homes beds", "")),
        "location_start_date": _s(row, "Location HSCA start date"),
        "location_end_date": _s(row, "Location HSCA end date"),
        "dormant": _s(row, "Dormant"),
        "latest_overall_rating": _s(row, "Location Latest Overall Rating"),
        "publication_date": _s(row, "Publication Date"),
        "service_users_supported": _s(row, "Service users supported"),
        "care_home_size_band": _s(row, "Size of care home (bands by number of beds)"),
        "location_length_service_band": _s(row, "Location length of service (bands by number of years)"),
        "safe_rating": _s(row, "Location safe rating"),
        "effective_rating": _s(row, "Location effective rating"),
        "caring_rating": _s(row, "Location caring rating"),
        "responsive_rating": _s(row, "Location responsive rating"),
        "well_led_rating": _s(row, "Location well-led rating"),
    }


def provider_companies_house_number(row: dict) -> str:
    """CH number for the provider, from a locations row ('' if absent)."""
    return _s(row, "Provider Companies House Number")


# Stable identity keys (also the diff keys in cqc_refresh).
def directory_provider_key(row: dict) -> str | None:
    return _s(row, "CQC Provider ID (for office use only)") or None


def directory_location_key(row: dict) -> str:
    return _s(row, "CQC Location ID (for office use only)")


def locations_location_key(row: dict) -> str:
    return _s(row, "Location ID")


def locations_provider_key(row: dict) -> str:
    return _s(row, "Provider ID")
