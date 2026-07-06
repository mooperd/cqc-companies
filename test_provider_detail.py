"""Offline test for the provider detail (people) view. Self-contained: forces an
in-memory SQLite DB before importing the app, seeds a provider + a scraped LinkedIn
person, and asserts the route renders them. Run: python test_provider_detail.py"""

import os

# Bind the app to in-memory SQLite before it's imported (app reads DATABASE_URL at
# import). load_dotenv() won't override an already-set env var.
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ.setdefault("SECRET_KEY", "test")

from app import app  # noqa: E402
from model import db, Provider, Person, Role  # noqa: E402


def test_provider_detail_shows_people():
    with app.app_context():
        db.create_all()
        prov = Provider(name="Barchester Healthcare Homes Limited",
                        linkedin_company_id="80128", active=True)
        db.session.add(prov)
        db.session.flush()
        person = Person(name="Bex A", linkedin_url="https://linkedin.com/in/bex-a",
                        match_confidence="low")
        db.session.add(person)
        db.session.flush()
        db.session.add(Role(
            person_id=person.id, provider_id=prov.id, role_type="influencer",
            source="phantombuster:linkedin-search-export", confidence="low",
            control_nature="Senior General Manager at Barchester Healthcare"))
        db.session.commit()
        pid = prov.id

        client = app.test_client()
        resp = client.get(f"/provider/{pid}")
        assert resp.status_code == 200, resp.status_code
        body = resp.data.decode()
        assert "Bex A" in body                                       # the person
        assert "Senior General Manager at Barchester Healthcare" in body  # headline
        assert "LinkedIn contacts" in body                           # the group label
        assert "https://linkedin.com/in/bex-a" in body               # clickable profile
        assert "LinkedIn company 80128" in body                      # resolved id badge

        # An unknown provider 404s.
        assert client.get("/provider/999999").status_code == 404
    print("OK — provider_detail: renders a provider's LinkedIn people; 404 on unknown")


def test_provider_detail_with_no_people():
    with app.app_context():
        db.create_all()
        prov = Provider(name="Lonely Provider Ltd", active=True)
        db.session.add(prov)
        db.session.commit()
        resp = app.test_client().get(f"/provider/{prov.id}")
        assert resp.status_code == 200
        assert "No people recorded" in resp.data.decode()
    print("OK — provider_detail: empty-state when a provider has no people")


if __name__ == "__main__":
    test_provider_detail_shows_people()
    test_provider_detail_with_no_people()
    print("\nAll provider_detail tests passed.")
