from src.services.candidate_validator import validate_candidates


def test_validator_does_not_readd_mismatches_when_few_candidates_remain():
    candidates = [
        {"content": "wrong company", "metadata": {"company": "microsoft", "fiscal_year": 2023}},
        {"content": "right", "metadata": {"company": "apple", "fiscal_year": 2023}},
        {"content": "wrong year", "metadata": {"company": "apple", "fiscal_year": 2022}},
    ]
    kept, diag = validate_candidates(candidates=candidates, target_companies=["apple"], target_fiscal_years=[2023])
    assert kept == [candidates[1]]
    assert [d["valid"] for d in diag] == [False, True, False]
    assert not any(d.get("relaxed") for d in diag)


def test_validator_keeps_requested_company_and_year_lists():
    candidates = [{"metadata": {"company": company, "fiscal_year": year}} for company, year in (
        ("microsoft", 2016), ("3m", 2023), ("microsoft", 2024), ("nike", 2023))]
    kept, _ = validate_candidates(candidates=candidates, target_companies=["microsoft", "3m"],
                                  target_fiscal_years=[2016, 2023])
    assert kept == candidates[:2]
