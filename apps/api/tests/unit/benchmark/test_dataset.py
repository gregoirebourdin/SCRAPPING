"""Ground-truth import: CSV column mapping, grouping, validation and limits; JSON items."""

from __future__ import annotations

import pytest

from scout.benchmark import dataset as ds
from scout.benchmark.dataset import DatasetError, map_headers, parse_csv, summarize, validate_items

CSV = (
    "Domain;Company;Prénom;Nom;Poste;Email;Status;enrich:uses_hubspot\n"
    "https://www.acme-demo.fr;Acme Demo;Marie;Dupont;Fondatrice;Marie.Dupont@acme-demo.fr;valid;oui\n"
    "acme-demo.fr;Acme Demo;Jean;Martin;Directeur commercial;jean.martin@acme-demo.fr;;\n"
    "acme-demo.fr;Acme Demo;Paul;Leroy;Comptable;;INVALID;\n"
    "globex-demo.com;Globex;John;Smith;CEO;john@globex-demo.com;catch-all;non\n"
)


def test_header_synonyms_and_enrich_columns() -> None:
    mapping, ignored = map_headers(
        ["Domain", "Prénom", "nom", "Job title", "E-mail", "enrich:Uses HubSpot", "Notes"]
    )
    assert mapping == {
        "Domain": "company_domain",
        "Prénom": "person_first",
        "nom": "person_last",
        "Job title": "person_title",
        "E-mail": "email",
        "enrich:Uses HubSpot": "enrich:Uses HubSpot",
    }
    assert ignored == ["Notes"]


def test_csv_rows_group_by_company_with_normalized_truth() -> None:
    parsed = parse_csv(CSV)
    assert parsed.rows == 4 and len(parsed.items) == 2
    acme, globex = parsed.items
    assert acme["label"] == "acme-demo.fr"
    assert acme["input"] == {"company": {"domain": "acme-demo.fr", "name": "Acme Demo"}}
    exp = acme["expected"]
    assert exp["company"] == {"domain": "acme-demo.fr", "name": "Acme Demo"}
    assert [p["first"] for p in exp["people"]] == ["Marie", "Jean", "Paul"]
    assert exp["people"][0]["title"] == "Fondatrice"
    assert exp["emails"][0] == {
        "address": "marie.dupont@acme-demo.fr",
        "status": "SAFE",
        "first": "Marie",
        "last": "Dupont",
    }
    assert exp["emails"][2] == {"status": "INVALID", "first": "Paul", "last": "Leroy"}  # no mailbox
    assert exp["enrichment"] == {"uses_hubspot": "oui"}
    assert exp["people_exhaustive"] is True
    assert globex["expected"]["emails"][0]["status"] == "CATCH_ALL"
    assert summarize(parsed.items) == {
        "items": 2,
        "people": 4,
        "emails": 3,
        "invalid_emails": 1,
        "enrichment_values": 2,
    }


def test_company_input_name_hides_the_domain_from_the_engine() -> None:
    parsed = parse_csv(
        "company_domain,company_name,company_city\nacme-demo.fr,Acme Demo,Lyon\n",
        company_input="name",
        people_exhaustive=False,
    )
    item = parsed.items[0]
    assert item["input"] == {"company": {"name": "Acme Demo", "city": "Lyon"}}
    assert item["expected"]["company"]["domain"] == "acme-demo.fr"
    assert item["expected"]["people_exhaustive"] is False
    assert "people" not in item["expected"]  # no person column: people are not evaluated


def test_email_kind_gives_people_identities_as_input() -> None:
    parsed = parse_csv(
        "company_domain,person_first,person_last,email\nacme-demo.fr,Marie,Dupont,marie@acme-demo.fr\n",
        kind="email",
    )
    assert parsed.items[0]["input"]["people"] == [{"first": "Marie", "last": "Dupont", "full_name": None}]


def test_full_name_column_is_split() -> None:
    parsed = parse_csv("company_domain,person_name\nacme-demo.fr,Jean-Pierre de la Fontaine\n")
    assert parsed.items[0]["expected"]["people"] == [{"first": "Jean-Pierre", "last": "de la Fontaine"}]


@pytest.mark.parametrize(
    ("csv_text", "fragment"),
    [
        ("company_domain,email\nacme-demo.fr,not-an-email\n", "Invalid address"),
        ("company_domain,email,email_status\nacme-demo.fr,a@acme-demo.fr,maybe\n", "Unknown status"),
        ("company_domain,person_first\n,Marie\n", "company_domain or company_name is required"),
        ("company_domain\nlocalhost\n", "Not a public domain"),
        ("company_domain,company_catch_all\nacme-demo.fr,perhaps\n", "true/false"),
        ("company_domain,enrich:x\nacme-demo.fr,a\nacme-demo.fr,b\n", "Conflicting values"),
    ],
)
def test_row_errors_carry_row_numbers(csv_text: str, fragment: str) -> None:
    with pytest.raises(DatasetError) as ei:
        parse_csv(csv_text)
    assert any(fragment in e["message"] for e in ei.value.errors), ei.value.errors
    assert all(e["where"].startswith(("row ", "company ")) for e in ei.value.errors)


def test_structural_errors_and_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(DatasetError, match="empty"):
        parse_csv("")
    with pytest.raises(DatasetError, match="company_domain or company_name"):
        parse_csv("person_first,person_last\nMarie,Dupont\n")
    monkeypatch.setattr(ds, "MAX_ROWS", 2)
    with pytest.raises(DatasetError, match="Too many rows"):
        parse_csv("company_domain\na-demo.fr\nb-demo.fr\nc-demo.fr\n")
    monkeypatch.setattr(ds, "MAX_CSV_BYTES", 10)
    with pytest.raises(DatasetError, match="too large"):
        parse_csv("company_domain\nacme-demo.fr\n")


def test_template_csv_parses() -> None:
    parsed = parse_csv(ds.TEMPLATE_CSV)
    assert len(parsed.items) == 2 and not parsed.warnings


def test_json_items_validation_and_derived_input() -> None:
    items = validate_items(
        [
            {
                "expected": {
                    "company": {"domain": "https://acme-demo.fr", "email_pattern": "prenom.nom"},
                    "people": [{"first": "Marie", "last": "Dupont"}],
                    "emails": [
                        {
                            "address": "MARIE.DUPONT@acme-demo.fr",
                            "status": "valid",
                            "first": "Marie",
                            "last": "Dupont",
                        }
                    ],
                    "enrichment": {"uses_hubspot": True, "tags": ["a", "b"]},
                }
            }
        ],
        kind="leads",
    )
    it = items[0]
    assert it["input"] == {"company": {"domain": "acme-demo.fr"}}
    assert it["expected"]["company"]["email_pattern"] == "{first}.{last}"
    assert it["expected"]["emails"][0]["address"] == "marie.dupont@acme-demo.fr"
    assert it["expected"]["emails"][0]["status"] == "SAFE"
    with pytest.raises(DatasetError) as ei:
        validate_items(
            [
                {"expected": {"company": {"domain": "acme-demo.fr"}, "enrichment": {"x": {"nested": 1}}}},
                {"expected": {}},
                {"expected": {"company": {"domain": "acme-demo.fr"}}, "unexpected": 1},
            ],
            kind="leads",
        )
    msgs = " | ".join(e["message"] for e in ei.value.errors)
    assert "scalars" in msgs and "Nothing expected" in msgs and "Extra inputs" in msgs
    with pytest.raises(DatasetError, match="no items"):
        validate_items([], kind="leads")
