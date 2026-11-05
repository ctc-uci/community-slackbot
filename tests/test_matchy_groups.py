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


def test_plan_group_sizes_prefers_threes_and_pairs_only_when_needed():
    assert plan_group_sizes(0) == []
    assert plan_group_sizes(1) == [1]
    assert plan_group_sizes(2) == [2]
    assert plan_group_sizes(4) == [2, 2]
    for n in range(2, 80):
        sizes = plan_group_sizes(n)
        assert sum(sizes) == n
        assert all(s in (2, 3) for s in sizes)
        twos = sizes.count(2)
        # pairs only when n isn't divisible by 3: one pair for n%3==2, two for n%3==1
        assert twos == {0: 0, 2: 1, 1: 2}[n % 3]


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
