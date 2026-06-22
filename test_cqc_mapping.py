#!/usr/bin/env python3
"""Tests for the shared CQC row→model field mappers (ADR 0015 WS3b).

Run: python test_cqc_mapping.py
"""

import cqc_mapping as cm


def test_provider_fields():
    row = {"Provider name": " Acme Care ", "CQC Provider ID (for office use only)": "1-101",
           "Website": "x.com"}
    assert cm.provider_fields(row) == {"name": "Acme Care", "cqc_provider_id": "1-101", "website": "x.com"}
    # Empty provider id → None (so keying never uses "").
    assert cm.provider_fields({"Provider name": "X"})["cqc_provider_id"] is None
    print("OK — provider_fields: strips, empty id → None")


def test_facility_fields():
    row = {"Name": "Home 1", "CQC Location ID (for office use only)": "1-201",
           "Postcode": "AB1 2CD", "Service types": "Care home"}
    f = cm.facility_fields(row)
    assert f["name"] == "Home 1" and f["cqc_location_id"] == "1-201"
    assert f["postcode"] == "AB1 2CD" and f["service_types"] == "Care home"
    assert f["email_address"] == "" and "provider_id" not in f
    print("OK — facility_fields: directory row → facility fields (no provider_id)")


def test_facility_enrichment_fields():
    row = {"Registered manager": "Jane", "Care homes beds": "12",
           "Location safe rating": "Good", "Dormant": "N"}
    e = cm.facility_enrichment_fields(row)
    assert e["registered_manager"] == "Jane"
    assert e["care_home_beds"] == 12        # parsed to int
    assert e["safe_rating"] == "Good" and e["dormant"] == "N"
    # Non-numeric / blank beds → None.
    assert cm.facility_enrichment_fields({"Care homes beds": ""})["care_home_beds"] is None
    assert cm.facility_enrichment_fields({"Care homes beds": "n/a"})["care_home_beds"] is None
    print("OK — facility_enrichment_fields: enrichment + bed-count parsing")


def test_keys():
    assert cm.directory_provider_key({"CQC Provider ID (for office use only)": "1-1"}) == "1-1"
    assert cm.directory_location_key({"CQC Location ID (for office use only)": "1-2"}) == "1-2"
    assert cm.locations_location_key({"Location ID": "1-3"}) == "1-3"
    assert cm.provider_companies_house_number({"Provider Companies House Number": "01234567"}) == "01234567"
    print("OK — key extractors")


if __name__ == "__main__":
    test_provider_fields()
    test_facility_fields()
    test_facility_enrichment_fields()
    test_keys()
    print("\nAll cqc_mapping tests passed.")
