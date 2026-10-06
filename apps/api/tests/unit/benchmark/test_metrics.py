"""Benchmark metrics: normalization, matching rules, per-item verdicts and aggregation (pure functions)."""

from __future__ import annotations

import pytest

from scout.benchmark.metrics import (
    aggregate,
    compare_company,
    compare_emails,
    compare_enrichment,
    compare_item,
    compare_people,
    infer_expected_pattern,
    name_tokens,
    names_match,
    norm_domain,
    norm_email,
    norm_pattern,
    norm_status,
    normalize_metrics,
    percentile,
    rate,
    roles_match,
    values_equal,
    wilson,
)

# ---- statistics -----------------------------------------------------------------------------


def test_wilson_interval_bounds_and_empty_sample() -> None:
    assert wilson(0, 0) is None
    lo, hi = wilson(9, 10)  # type: ignore[misc]
    assert 0.6 < lo < 0.9 < hi <= 1.0
    lo0, hi0 = wilson(0, 5)  # type: ignore[misc]
    assert lo0 == 0.0 and 0 < hi0 < 0.5
    lo1, hi1 = wilson(5, 5)  # type: ignore[misc]
    assert hi1 == 1.0 and lo1 > 0.5
    # more data → narrower interval around the same proportion
    w_small, w_big = wilson(8, 10), wilson(800, 1000)
    assert w_small and w_big and (w_big[1] - w_big[0]) < (w_small[1] - w_small[0])


def test_percentile_linear_interpolation() -> None:
    assert percentile([], 50) is None
    assert percentile([7], 95) == 7
    assert percentile([1, 2, 3, 4], 50) == 2.5
    assert percentile([10, 20, 30, 40, 50], 95) == pytest.approx(48.0)


def test_rate_helper_reports_sample_size() -> None:
    r = rate(3, 4)
    assert r["value"] == 0.75 and r["k"] == 3 and r["n"] == 4 and len(r["ci90"]) == 2
    assert rate(0, 0) == {"value": None, "k": 0, "n": 0, "ci90": None}


# ---- normalization & matching -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://www.Acme-Demo.fr/contact", "acme-demo.fr"),
        ("acme-demo.fr", "acme-demo.fr"),
        ("marie@acme-demo.fr", "acme-demo.fr"),
        ("shop.acme-demo.co.uk", "acme-demo.co.uk"),
        ("", None),
        (None, None),
    ],
)
def test_domain_normalization(raw: str | None, expected: str | None) -> None:
    assert norm_domain(raw) == expected


def test_email_and_status_normalization() -> None:
    assert norm_email(" Mailto:Marie.Dupont@ACME-demo.fr ") == "marie.dupont@acme-demo.fr"
    assert norm_email("not-an-email") is None
    assert norm_email("a@b@c.fr") is None
    assert norm_status("valid") == "SAFE"
    assert norm_status("catch-all") == "CATCH_ALL"
    assert norm_status("likely_safe") == "LIKELY_SAFE"
    assert norm_status("bounce") == "INVALID"
    assert norm_status("whatever") is None
    assert norm_pattern("prenom.nom") == "{first}.{last}"
    assert norm_pattern("f.last") == "{f}.{last}"
    assert norm_pattern("{first}") == "{first}"


def test_names_match_accents_order_hyphens_and_middle_names() -> None:
    assert names_match(name_tokens("Élodie", "Lefèbvre-Durand"), name_tokens(full="elodie lefebvre durand"))
    assert names_match(name_tokens("Marie", "Dupont"), name_tokens(full="DUPONT Marie"))
    assert names_match(name_tokens("Anne", "Roux"), name_tokens(full="Anne Marie Roux"))
    assert names_match(name_tokens(full="Mme Marie Dupont"), name_tokens("Marie", "Dupont"))
    assert not names_match(name_tokens("Marie", "Dupont"), name_tokens("Marine", "Dupont"))
    # a single shared token is never enough
    assert not names_match(name_tokens(full="Dupont"), name_tokens("Marie", "Dupont"))
    assert not names_match((), ())


def test_roles_match_via_title_normalizer() -> None:
    assert roles_match("Fondatrice", "Founder")
    assert roles_match("Gérant", "CEO")  # both head the company
    assert roles_match("Directeur commercial", "Head of Sales")  # same family, same seniority band
    assert not roles_match("CTO", "Directeur technique")
    assert not roles_match("Head of Sales", "Sales Assistant")
    assert not roles_match("Marketing manager", "CEO")


@pytest.mark.parametrize(
    ("expected", "actual", "dtype", "ok"),
    [
        (True, "oui", None, True),
        ("true", False, None, False),
        ("yes", True, "boolean", True),
        ("12", "12.4", None, True),  # within 5 %
        ("12", "14", None, False),
        ("1 200", 1200, "number", True),
        ("12k", "12000", None, True),
        ("Agence web", "agence  WEB!", None, True),
        ("SaaS", "saas", "enum", True),
        ("https://www.acme-demo.fr/", "acme-demo.fr", "url", True),
        (["HubSpot", "Stripe"], ["stripe", "hubspot"], None, True),
        ("Paris", None, None, False),
        ("Marketing agency", "Software publisher", None, False),
    ],
)
def test_enrichment_value_equality(expected, actual, dtype, ok) -> None:
    assert values_equal(expected, actual, dtype) is ok


# ---- per-item verdicts ------------------------------------------------------------------------


def test_company_identity_given_wrong_and_missing() -> None:
    exp = {"domain": "acme-demo.fr"}
    given = compare_company(exp, {"found": True, "domain": "acme-demo.fr"}, {"domain": "acme-demo.fr"})
    assert given and given["given"] and given["tp"] == 1
    found = compare_company(exp, {"found": True, "domain": "acme-demo.fr"}, {"name": "Acme"})
    assert found and not found["given"] and (found["tp"], found["fp"], found["fn"]) == (1, 0, 0)
    wrong = compare_company(exp, {"found": True, "domain": "acme.com"}, {"name": "Acme"})
    assert wrong and (wrong["tp"], wrong["fp"], wrong["fn"]) == (0, 1, 1)
    missing = compare_company(exp, {"found": False}, {"name": "Acme"})
    assert missing and (missing["tp"], missing["fp"], missing["fn"]) == (0, 0, 1)
    by_name = compare_company({"name": "Acme SAS"}, {"found": True, "name": "ACME"}, {"name": "Acme"})
    assert by_name and by_name["tp"] == 1
    assert compare_company(None, None, None) is None
    assert compare_company({}, {"found": True}, {}) is None


def test_people_matching_exhaustive_vs_partial_and_duplicates() -> None:
    expected = [{"first": "Marie", "last": "Dupont", "title": "Fondatrice"}, {"first": "Jean", "last": "Martin"}]
    actual = [
        {"first": "Marie", "last": "Dupont", "title": "CEO"},
        {"full_name": "DUPONT Marie"},  # duplicate of the first (order swapped)
        {"first": "Zoé", "last": "Bernard"},
    ]
    v, pairs = compare_people(expected, actual, exhaustive=True)
    assert v is not None
    assert pairs == [(0, 0)]
    assert (v["tp"], v["fp"], v["fn"]) == (1, 2, 1)
    assert v["duplicates"] == 1 and v["roles_evaluated"] == 1 and v["roles_correct"] == 1
    assert v["missing"] == ["Jean Martin"]
    partial, _ = compare_people(expected, actual, exhaustive=False)
    assert partial is not None and partial["fp"] == 0 and partial["unscored"] == 2
    empty, _ = compare_people([], [], exhaustive=True)
    assert empty is not None and (empty["tp"], empty["fp"], empty["fn"]) == (0, 0, 0)
    assert compare_people(None, actual)[0] is None


def test_email_verdicts_including_invalid_and_company_level() -> None:
    exp_people = [{"first": "Marie", "last": "Dupont"}, {"first": "Jean", "last": "Martin"}, {"first": "Paul", "last": "Leroy"}]
    actual = [
        {"first": "Marie", "last": "Dupont", "email": {"address": "Marie.Dupont@acme-demo.fr", "status": "SAFE"}},
        {"first": "Jean", "last": "Martin", "email": {"address": "j.martin@acme-demo.fr", "status": "LIKELY_SAFE"}},
        {"first": "Paul", "last": "Leroy", "email": {"address": "paul@acme-demo.fr", "status": "SAFE"}},
    ]
    _, pairs = compare_people(exp_people, actual)
    exp_emails = [
        {"address": "marie.dupont@acme-demo.fr", "status": "SAFE", "first": "Marie", "last": "Dupont"},
        {"address": "jean.martin@acme-demo.fr", "first": "Jean", "last": "Martin"},
        {"status": "INVALID", "first": "Paul", "last": "Leroy"},
        {"address": "contact@acme-demo.fr"},
        {"address": "ann@acme-demo.fr", "first": "Ann", "last": "Lee"},
    ]
    v = compare_emails(exp_emails, exp_people, actual, [{"address": "contact@acme-demo.fr", "status": "UNKNOWN"}], pairs)
    assert v is not None
    verdicts = [r["verdict"] for r in v["rows"]]
    assert verdicts == ["correct", "wrong", "invalid_fp", "correct", "missing"]
    assert v["rows"][2]["wrong_claim"] is True
    assert (v["tp"], v["fp"], v["fn"]) == (2, 2, 2)
    assert compare_emails(None, exp_people, actual, [], pairs) is None


def test_invalid_address_rejected_by_engine_is_not_a_false_positive() -> None:
    actual = [{"first": "Paul", "last": "Leroy", "email": {"address": "paul@acme-demo.fr", "status": "INVALID"}}]
    exp = [{"address": "paul@acme-demo.fr", "status": "INVALID", "first": "Paul", "last": "Leroy"}]
    v = compare_emails(exp, None, actual, [], [])
    assert v and v["rows"][0]["verdict"] == "invalid_ok" and v["fp"] == 0


def test_enrichment_verdicts() -> None:
    v = compare_enrichment(
        {"uses_hubspot": "true", "employees": "50", "industry": "Agence web", "empty": ""},
        {
            "uses_hubspot": {"status": "success", "value": True, "data_type": "boolean"},
            "employees": {"status": "success", "value": 80},
            "industry": {"status": "unknown", "value": None},
        },
    )
    assert v is not None
    assert [r["verdict"] for r in v["rows"]] == ["correct", "wrong", "unknown"]
    assert (v["evaluated"], v["answered"], v["correct"], v["fp"], v["fn"]) == (3, 2, 1, 1, 2)
    assert compare_enrichment({}, {}) is None


def test_expected_pattern_inferred_from_named_addresses_and_ties_are_unknown() -> None:
    emails = [
        {"address": "marie.dupont@acme-demo.fr", "first": "Marie", "last": "Dupont"},
        {"address": "jean.martin@acme-demo.fr", "first": "Jean", "last": "Martin"},
        {"address": "x@acme-demo.fr", "status": "INVALID", "first": "Paul", "last": "X"},
    ]
    assert infer_expected_pattern({}, emails) == "{first}.{last}"
    assert infer_expected_pattern({"email_pattern": "prenom"}, emails) == "{first}"
    tie = [
        {"address": "marie.dupont@acme-demo.fr", "first": "Marie", "last": "Dupont"},
        {"address": "jmartin@acme-demo.fr", "first": "Jean", "last": "Martin"},
    ]
    assert infer_expected_pattern({}, tie) is None


def test_catch_all_truth_from_status_and_unknown_engine_verdict_counts_as_wrong() -> None:
    expected = {
        "company": {"domain": "acme-demo.fr"},
        "emails": [{"address": "marie@acme-demo.fr", "status": "CATCH_ALL", "first": "Marie", "last": "Dupont"}],
    }
    unknown = compare_item(expected, {"company": {"found": True, "domain": "acme-demo.fr"}, "domain": {}})
    assert unknown["catch_all"] == {"expected": True, "actual": None, "correct": False}
    from_status = compare_item(
        expected,
        {
            "company": {"found": True, "domain": "acme-demo.fr"},
            "people": [{"first": "Marie", "last": "Dupont", "email": {"address": "marie@acme-demo.fr", "status": "CATCH_ALL"}}],
            "domain": {"catch_all": None, "pattern": "{first}"},
        },
    )
    assert from_status["catch_all"]["correct"] is True
    assert from_status["pattern"] == {"expected": "{first}", "actual": "{first}", "correct": True}
    explicit = compare_item({"company": {"domain": "acme-demo.fr", "catch_all": False}}, {"domain": {"catch_all": True}})
    assert explicit["catch_all"]["correct"] is False


# ---- aggregation ------------------------------------------------------------------------------


def test_aggregate_empty_run_reports_no_rates() -> None:
    m = aggregate([], mode="registry")
    assert m["items"]["value"] == 0
    assert "person_precision" not in m and "email_precision" not in m
    assert m["avg_processing_ms"]["value"] is None
    assert m["cost_per_qualified_lead"]["value"] is None


def _item(**actual_overrides):
    expected = {
        "company": {"domain": "acme-demo.fr"},
        "people": [{"first": "Marie", "last": "Dupont", "title": "CEO"}],
        "people_exhaustive": True,
        "emails": [{"address": "marie.dupont@acme-demo.fr", "first": "Marie", "last": "Dupont"}],
        "enrichment": {"uses_hubspot": True},
    }
    actual = {
        "company": {"found": True, "domain": "acme-demo.fr"},
        "people": [
            {
                "first": "Marie",
                "last": "Dupont",
                "title": "Présidente",
                "email": {"address": "marie.dupont@acme-demo.fr", "status": "SAFE"},
            }
        ],
        "enrichment": {"uses_hubspot": {"status": "success", "value": True}},
        "email_resolutions": [{"ms": 100, "deep": False, "cache_hit": True}],
        **actual_overrides,
    }
    return expected, actual


def test_aggregate_rates_carry_n_and_interval_and_given_identity_is_excluded() -> None:
    e1, a1 = _item()
    e2, a2 = _item(
        people=[{"first": "Zoé", "last": "Bernard", "email": {"address": "zoe@acme-demo.fr", "status": "SAFE"}}],
        email_resolutions=[{"ms": 300, "deep": True, "cache_hit": False}],
    )
    outcomes = [
        {"verdicts": compare_item(e1, a1, {"company": {"domain": "acme-demo.fr"}}), "latency_ms": 100, "cost_usd": 0.01},
        {"verdicts": compare_item(e2, a2, {"company": {"name": "Acme"}}), "latency_ms": 300, "cost_usd": 0.03},
    ]
    m = aggregate(outcomes, mode="live", total_cost_usd=0.04, duration_ms=60_000)
    # company: item 1 identity was given (excluded), item 2 discovered → 1/1
    assert m["company_identity_given"]["value"] == 1
    assert (m["company_precision"]["k"], m["company_precision"]["n"]) == (1, 1)
    assert m["person_precision"]["value"] == 0.5 and m["person_precision"]["n"] == 2
    assert m["person_recall"]["value"] == 0.5 and m["person_recall"]["ci90"] is not None
    assert m["role_precision"]["value"] == 1.0
    assert m["email_precision"]["value"] == 1.0 and m["email_recall"]["value"] == 0.5
    assert m["safe_email_precision"]["value"] == 1.0
    assert m["smtp_fallback_rate"]["value"] == 0.5 and m["cache_hit_rate"]["value"] == 0.5
    assert m["avg_email_resolution_ms"]["value"] == 200 and m["p95_email_resolution_ms"]["value"] == pytest.approx(290)
    assert m["enrichment_accuracy"]["value"] == 1.0
    assert m["qualified_leads"]["value"] == 1
    assert m["cost_per_qualified_lead"]["value"] == pytest.approx(0.04)
    assert m["emails_resolved_per_minute"]["value"] == pytest.approx(1.0)
    assert m["false_positives"]["value"] == 1 and m["false_negatives"]["value"] == 2
    assert m["avg_processing_ms"]["value"] == 200
    for metric in m.values():  # only measured numbers: every rate states its sample size
        if metric["unit"] == "rate":
            assert metric["n"] is not None


def test_aggregate_registry_mode_has_coverage_but_no_throughput() -> None:
    e, a = _item()
    m = aggregate([{"verdicts": compare_item(e, a, {"company": {"domain": "acme-demo.fr"}})}], mode="registry")
    assert m["company_coverage"]["value"] == 1.0
    assert "emails_resolved_per_minute" not in m and "cost_per_email" not in m
    assert m["cost_usd"]["value"] == 0


def test_duplicate_rate_counts_people_and_shared_addresses() -> None:
    e, a = _item(
        people=[
            {"first": "Marie", "last": "Dupont", "email": {"address": "marie@acme-demo.fr", "status": "SAFE"}},
            {"full_name": "Dupont Marie", "email": {"address": "marie@acme-demo.fr", "status": "SAFE"}},
        ]
    )
    m = aggregate([{"verdicts": compare_item(e, a)}], mode="registry")
    assert m["duplicate_rate"]["k"] == 2 and m["duplicate_rate"]["n"] == 4


def test_normalize_suite_metrics() -> None:
    out = normalize_metrics(
        {
            "email_precision": 0.9,
            "recall_any_status": rate(8, 10),
            "p95_resolution_ms": 812.4,
            "cost_per_email_usd": 0.0012,
            "smtp_rcpts": 42,
            "statuses": {"SAFE": 3},  # breakdowns are ignored
            "flag": True,
        },
        samples={"email_precision": 20},
        definitions={"email_precision": "SAFE + LIKELY_SAFE only"},
    )
    assert "statuses" not in out
    assert out["email_precision"]["unit"] == "rate" and out["email_precision"]["n"] == 20
    assert out["email_precision"]["k"] == 18 and out["email_precision"]["ci90"]
    assert out["email_precision"]["definition"] == "SAFE + LIKELY_SAFE only"
    assert out["recall_any_status"]["unit"] == "rate" and out["recall_any_status"]["n"] == 10
    assert out["p95_resolution_ms"]["unit"] == "ms"
    assert out["cost_per_email_usd"]["unit"] == "usd"
    assert out["smtp_rcpts"]["unit"] == "number" and out["smtp_rcpts"]["definition"]
    assert out["flag"]["value"] == 1
