"""Regression tests for Matchy group assignment sanitization."""
from features.matchy_core import (
    ensure_no_single_groups,
    sanitize_match_groups,
    validate_match_assignments,
)


def test_sanitize_removes_duplicate_in_group():
    matches = [["A", "B", "A"], ["C", "D"]]
    cleaned = sanitize_match_groups(matches)
    assert cleaned == [["A", "B"], ["C", "D"]]


def test_ensure_no_single_groups_no_duplicate_when_splitting_three_person_group():
    """Lone member must not be merged into a 3-person group they already belong to."""
    previous = {}
    # Repro: singles queue has m1 while result still has [m1, m2, m3]
    matches = [["m1", "m2", "m3"], ["m1"]]
    result = ensure_no_single_groups(matches, previous, allow_repeats=True)
    for group in result:
        assert len(group) == len(set(group))
    assert validate_match_assignments(result, {"members": []})["ok"]


def test_validate_rejects_duplicate_in_group():
    bad = [["U1", "U2", "U1"]]
    out = validate_match_assignments(bad, {"members": [{"slackId": "U1", "name": "One"}]})
    assert not out["ok"]
