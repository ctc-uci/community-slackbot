"""Regression tests for Matchy group assignment."""
from features.matchy.matching import (
    ensure_no_single_groups,
    merge_stragglers,
    plan_group_sizes,
    sanitize_groups,
    validate_match_assignments,
)

sanitize_match_groups = sanitize_groups


def test_sanitize_removes_duplicate_in_group():
    matches = [["A", "B", "A"], ["C", "D"]]
    assert sanitize_groups(matches) == [["A", "B"], ["C", "D"]]


def test_ensure_no_single_groups_no_duplicate_when_splitting_three_person_group():
    previous = {}
    matches = [["m1", "m2", "m3"], ["m1"]]
    result = ensure_no_single_groups(matches, previous, allow_repeats=True)
    for group in result:
        assert len(group) == len(set(group))
    assert validate_match_assignments(result, {"members": []})["ok"]


def test_plan_group_sizes_balances_twos_and_threes():
    sizes = plan_group_sizes(33)
    assert sum(sizes) == 33
    twos = sum(1 for s in sizes if s == 2)
    threes = sum(1 for s in sizes if s == 3)
    assert abs(twos - threes) <= 1
    assert twos >= 1 and threes >= 1


def test_merge_stragglers_places_solo_into_existing_pair():
    matches = [["A", "B"], ["C", "D"], ["solo"]]
    merged, unmatched = merge_stragglers(matches)
    assert not unmatched
    assert all(len(g) >= 2 for g in merged)
    assert sum(len(g) for g in merged) == 5


def test_validate_rejects_duplicate_in_group():
    bad = [["U1", "U2", "U1"]]
    out = validate_match_assignments(bad, {"members": [{"slackId": "U1", "name": "One"}]})
    assert not out["ok"]
