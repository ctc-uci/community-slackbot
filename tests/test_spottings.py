"""
Rigorous tests for features/spottings.py.

Covers:
- _extract_mentions
- _apply_cooldown
- _compute_counts_from_messages
- _build_leaderboard_blocks
- _seconds_until_next_run
- _fetch_channel_messages
- _get_user_counts / _set_user_count
- _run_nightly_count
- _post_leaderboard
- register_spottings_handlers: /edit-spotting, /edit-spotted (admin gate)
- handle_spottings_select action handler
- handle_spottings_edit_submit view submission handler

No real Firebase credentials or Slack tokens are required.
All external I/O is mocked.
"""

import sys
import os
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

import pytest
import pytz

# ---------------------------------------------------------------------------
# Add project root to import path so "features" and "firebase_client" resolve.
# firebase_admin is installed; firebase_client is in project root.
# We do a direct import so features.spottings stays in sys.modules, which is
# required for subsequent patch() calls to target the correct module globals.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from features.spottings import (  # noqa: E402
    _extract_mentions,
    _apply_cooldown,
    _compute_counts_from_messages,
    _build_leaderboard_blocks,
    _seconds_until_next_run,
    _fetch_channel_messages,
    _get_user_counts,
    _set_user_count,
    _run_nightly_count,
    _post_leaderboard,
    register_spottings_handlers,
    ADMIN_USER_IDS,
)


# ---------------------------------------------------------------------------
# Helper: a minimal mock Bolt App that captures registered handlers
# ---------------------------------------------------------------------------

class CapturingApp:
    """Minimal mock of slack_bolt.App that records handlers registered via decorators."""

    def __init__(self):
        self.commands = {}   # pattern -> callable
        self.actions = {}    # pattern -> callable
        self.views = {}      # callback_id -> callable

    def command(self, pattern):
        def decorator(f):
            self.commands[pattern] = f
            return f
        return decorator

    def action(self, pattern):
        def decorator(f):
            self.actions[pattern] = f
            return f
        return decorator

    def view(self, pattern):
        def decorator(f):
            self.views[pattern] = f
            return f
        return decorator


def _make_registered_app():
    """Return a CapturingApp with all spottings handlers registered (scheduler suppressed)."""
    app = CapturingApp()
    # Patch threading.Thread so the nightly scheduler doesn't actually start.
    with patch("threading.Thread"):
        register_spottings_handlers(app)
    return app


# ---------------------------------------------------------------------------
# Helper: build a Firestore mock that returns controllable document data
# ---------------------------------------------------------------------------

def _make_firestore_mock(doc_data=None, doc_exists=True):
    """Return (mock_db, mock_doc_ref) with a configurable Firestore document."""
    mock_doc = MagicMock()
    mock_doc.exists = doc_exists
    if doc_data is not None:
        mock_doc.to_dict.return_value = doc_data

    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = mock_doc

    mock_db = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc_ref
    return mock_db, mock_doc_ref


# ===========================================================================
# 1. _extract_mentions
# ===========================================================================

class TestExtractMentions:
    def test_single_mention(self):
        assert _extract_mentions("<@U123>") == {"U123"}

    def test_mention_with_display_name(self):
        assert _extract_mentions("<@U123|john>") == {"U123"}

    def test_multiple_distinct_mentions(self):
        assert _extract_mentions("<@UA> and <@UB>") == {"UA", "UB"}

    def test_duplicate_mention_deduped_by_set(self):
        # _extract_mentions returns a set, so same user twice → single entry
        assert _extract_mentions("<@UA> <@UA>") == {"UA"}

    def test_no_mentions(self):
        assert _extract_mentions("hello world") == set()

    def test_empty_string(self):
        assert _extract_mentions("") == set()

    def test_none_returns_empty(self):
        assert _extract_mentions(None) == set()

    def test_mixed_text_and_mention(self):
        result = _extract_mentions("spotted <@UABC123|alice> at the library!")
        assert result == {"UABC123"}

    def test_multiple_mentions_with_display_names(self):
        result = _extract_mentions("<@U001|bob> <@U002|carol> hey!")
        assert result == {"U001", "U002"}


# ===========================================================================
# 2. _apply_cooldown
# ===========================================================================

class TestApplyCooldown:
    def test_empty_input(self):
        assert _apply_cooldown([]) == []

    def test_single_pair_passes(self):
        result = _apply_cooldown([("UA", "UB", 1000.0)])
        assert result == [("UA", "UB")]

    def test_same_pair_within_30s_deduped(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1020.0)]  # 20s apart
        result = _apply_cooldown(pairs)
        assert len(result) == 1
        assert result[0] == ("UA", "UB")

    def test_same_pair_exactly_30s_counts_again(self):
        # ts - prev == 30 satisfies >= 30, so this DOES count as a new spotting
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1030.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2

    def test_same_pair_31s_apart_counts_twice(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1031.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2

    def test_different_pairs_within_30s_both_counted(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UC", 1005.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2
        assert ("UA", "UB") in result
        assert ("UA", "UC") in result

    def test_different_spotters_same_spotted_both_counted(self):
        pairs = [("UA", "UC", 1000.0), ("UB", "UC", 1005.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2

    def test_out_of_order_timestamps_sorted_before_cooldown(self):
        # ts=1020 appears first; ts=1000 appears second. After sorting:
        # 1000 (counted), 1020 is 20s later → within cooldown → skipped.
        pairs = [("UA", "UB", 1020.0), ("UA", "UB", 1000.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 1

    def test_three_events_first_two_deduped_third_counted(self):
        # t=0 counted, t=10 within 30s → skipped, t=40 is 40s from t=0 → counted
        pairs = [("UA", "UB", 0.0), ("UA", "UB", 10.0), ("UA", "UB", 40.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2

    def test_same_pair_at_same_timestamp_deduped(self):
        # Two entries with identical ts → second has ts - prev = 0 < 30 → skipped
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1000.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 1

    def test_custom_cooldown_respected(self):
        # With cooldown_sec=60, a pair 45s apart should be deduped
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1045.0)]
        result = _apply_cooldown(pairs, cooldown_sec=60)
        assert len(result) == 1

    def test_multiple_independent_pairs_all_counted(self):
        pairs = [
            ("U1", "U2", 100.0),
            ("U3", "U4", 100.0),
            ("U5", "U6", 100.0),
        ]
        result = _apply_cooldown(pairs)
        assert len(result) == 3


# ===========================================================================
# 3. _compute_counts_from_messages
# ===========================================================================

class TestComputeCountsFromMessages:
    def test_empty_messages(self):
        spotting, spotted = _compute_counts_from_messages([])
        assert spotting == {}
        assert spotted == {}

    def test_message_without_user_skipped(self):
        msgs = [{"text": "<@UB>", "ts": "1000.0"}]  # no "user" key
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {}
        assert spotted == {}

    def test_message_without_mentions_skipped(self):
        msgs = [{"user": "UA", "text": "hello!", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {}
        assert spotted == {}

    def test_self_mention_excluded(self):
        msgs = [{"user": "UA", "text": "<@UA>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {}
        assert spotted == {}

    def test_basic_one_spotter_one_spotted(self):
        msgs = [{"user": "UA", "text": "<@UB>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}

    def test_one_message_two_distinct_mentions(self):
        msgs = [{"user": "UA", "text": "<@UB> <@UC>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 2}
        assert spotted == {"UB": 1, "UC": 1}

    def test_same_person_mentioned_twice_in_one_message_counts_once(self):
        # _extract_mentions returns a set, <@UB> <@UB> → {"UB"} → one pair
        msgs = [{"user": "UA", "text": "<@UB> <@UB>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}

    def test_two_messages_same_pair_within_30s_counts_once(self):
        msgs = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UB>", "ts": "1020.0"},  # 20s later
        ]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}

    def test_two_messages_same_pair_after_30s_counts_twice(self):
        msgs = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UB>", "ts": "1031.0"},  # 31s later
        ]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 2}
        assert spotted == {"UB": 2}

    def test_multiple_spotters_multiple_spotted(self):
        msgs = [
            {"user": "UA", "text": "<@UC>", "ts": "1000.0"},
            {"user": "UB", "text": "<@UC>", "ts": "2000.0"},
            {"user": "UA", "text": "<@UD>", "ts": "3000.0"},
        ]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 2, "UB": 1}
        assert spotted == {"UC": 2, "UD": 1}

    def test_self_mention_mixed_with_real_mention(self):
        # UA mentions themselves AND UB — self-mention excluded, UB counts
        msgs = [{"user": "UA", "text": "<@UA> <@UB>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}
        assert "UA" not in spotted


# ===========================================================================
# 4. _build_leaderboard_blocks
# ===========================================================================

class TestBuildLeaderboardBlocks:
    def test_both_empty(self):
        blocks = _build_leaderboard_blocks([], [])
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        assert any("_No spottings yet!_" in t for t in section_texts)
        assert any("_No one spotted yet!_" in t for t in section_texts)

    def test_spotting_populated_spotted_empty(self):
        blocks = _build_leaderboard_blocks([("UA", 3)], [])
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        full_text = "\n".join(section_texts)
        assert "<@UA>" in full_text
        assert "_No one spotted yet!_" in full_text

    def test_both_populated_correct_rank_format(self):
        spotting_lb = [("UA", 5), ("UB", 3)]
        spotted_lb = [("UC", 7), ("UD", 2)]
        blocks = _build_leaderboard_blocks(spotting_lb, spotted_lb)
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        full_text = "\n".join(section_texts)
        assert "1. <@UA> \u2014 5" in full_text
        assert "2. <@UB> \u2014 3" in full_text
        assert "1. <@UC> \u2014 7" in full_text
        assert "2. <@UD> \u2014 2" in full_text

    def test_only_top_10_shown(self):
        spotting_lb = [(f"U{i:03d}", 100 - i) for i in range(15)]
        blocks = _build_leaderboard_blocks(spotting_lb, [])
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        full_text = "\n".join(section_texts)
        assert "10. " in full_text
        assert "11. " not in full_text

    def test_block_structure_has_header_and_divider(self):
        blocks = _build_leaderboard_blocks([("UA", 1)], [("UB", 2)])
        types = [b["type"] for b in blocks]
        assert "header" in types
        assert "divider" in types

    def test_header_contains_leaderboard_title(self):
        blocks = _build_leaderboard_blocks([], [])
        headers = [b for b in blocks if b["type"] == "header"]
        assert len(headers) == 1
        assert "CTC Spottings Leaderboard" in headers[0]["text"]["text"]

    def test_section_labels_present(self):
        blocks = _build_leaderboard_blocks([("UA", 1)], [("UB", 2)])
        section_texts = [
            b["text"]["text"] for b in blocks if b.get("type") == "section"
        ]
        full_text = "\n".join(section_texts)
        assert "Most Spottings Posted" in full_text
        assert "Most Spotted" in full_text


# ===========================================================================
# 5. _seconds_until_next_run
# ===========================================================================

class TestSecondsUntilNextRun:
    """
    Uses a FakeDatetime injected via patch to control 'now' without depending
    on the real wall clock. The patch target is 'features.spottings.datetime'
    because spottings.py imports `from datetime import datetime`.
    """

    TZ = pytz.timezone("America/Los_Angeles")

    def _fake_now(self, hour, minute, second=0):
        return self.TZ.localize(datetime(2024, 6, 15, hour, minute, second))

    def _run_with_fake_now(self, now_dt, target_hour, target_minute):
        class FakeDatetime:
            @staticmethod
            def now(tz):
                return now_dt

        with patch("features.spottings.datetime", FakeDatetime):
            return _seconds_until_next_run(target_hour, target_minute)

    def test_target_in_future_today(self):
        # now = 10:00, target = 23:59 → 13h59m = 50340s
        result = self._run_with_fake_now(self._fake_now(10, 0), 23, 59)
        expected = (23 - 10) * 3600 + 59 * 60
        assert abs(result - expected) < 2

    def test_target_already_passed_today_schedules_tomorrow(self):
        # now = 23:59:30, target = 23:59 (already past) → ~23h59m30s ≈ 86370s
        now_dt = self.TZ.localize(datetime(2024, 6, 15, 23, 59, 30))
        result = self._run_with_fake_now(now_dt, 23, 59)
        assert 86300 < result < 86400

    def test_target_exactly_now_schedules_tomorrow(self):
        # now = 23:59:00 == target → now >= target → +1 day = 86400s
        now_dt = self.TZ.localize(datetime(2024, 6, 15, 23, 59, 0))
        result = self._run_with_fake_now(now_dt, 23, 59)
        assert abs(result - 86400) < 2

    def test_midnight_target_from_morning(self):
        # now = 08:00, target = 00:00 (already past) → next midnight ≈ 16h = 57600s
        result = self._run_with_fake_now(self._fake_now(8, 0), 0, 0)
        expected = (24 - 8) * 3600
        assert abs(result - expected) < 2

    def test_result_is_always_positive(self):
        for hour in [0, 6, 12, 18, 23]:
            result = self._run_with_fake_now(self._fake_now(hour, 30), 23, 59)
            assert result > 0


# ===========================================================================
# 6. _fetch_channel_messages
# ===========================================================================

class TestFetchChannelMessages:
    def _msg(self, user="U1", text="hello", ts="1000.0", **extra):
        return {"user": user, "text": text, "ts": ts, **extra}

    def test_single_page_returns_messages(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [self._msg()],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 1
        assert result[0]["user"] == "U1"

    def test_bot_messages_excluded(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [
                self._msg(user="U1"),
                self._msg(user="BOT", bot_id="B123"),
            ],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 1
        assert result[0]["user"] == "U1"

    def test_messages_without_text_excluded(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [
                {"user": "U1", "ts": "1000.0"},           # missing "text"
                {"user": "U2", "text": "", "ts": "2000.0"},  # empty text falsy
                self._msg(user="U3"),
            ],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 1
        assert result[0]["user"] == "U3"

    def test_channel_join_leave_bot_message_subtypes_excluded(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [
                self._msg(user="U1", subtype="channel_join"),
                self._msg(user="U2", subtype="channel_leave"),
                self._msg(user="U3", subtype="bot_message"),
                self._msg(user="U4"),  # normal → included
            ],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 1
        assert result[0]["user"] == "U4"

    def test_pagination_fetches_all_pages(self):
        mock_client = MagicMock()
        mock_client.conversations_history.side_effect = [
            {
                "messages": [self._msg(user="U1", ts="1000.0")],
                "has_more": True,
                "response_metadata": {"next_cursor": "cursor_abc"},
            },
            {
                "messages": [self._msg(user="U2", ts="2000.0")],
                "has_more": False,
            },
        ]
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 2
        # Second API call must include the cursor
        second_call_kwargs = mock_client.conversations_history.call_args_list[1][1]
        assert second_call_kwargs.get("cursor") == "cursor_abc"

    def test_empty_channel_returns_empty_list(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert result == []

    def test_normal_subtype_message_included(self):
        # A message with an unrecognized subtype should still be included
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [self._msg(user="U1", subtype="")],
            "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C_TEST")
        assert len(result) == 1


# ===========================================================================
# 7. _get_user_counts / _set_user_count
# ===========================================================================

class TestGetUserCounts:
    def test_doc_exists_returns_counts(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 5, "spotted_count": 3})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U123")
        assert result == {"spotting_count": 5, "spotted_count": 3}

    def test_doc_missing_returns_zeros(self):
        mock_db, _ = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U_NEW")
        assert result == {"spotting_count": 0, "spotted_count": 0}

    def test_partial_data_defaults_missing_field_to_zero(self):
        # Doc exists but only has spotting_count
        mock_db, _ = _make_firestore_mock({"spotting_count": 7})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U123")
        assert result["spotting_count"] == 7
        assert result["spotted_count"] == 0

    def test_queries_correct_collection_and_document(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 0, "spotted_count": 0})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _get_user_counts("U_SPECIFIC")
        mock_db.collection.assert_called_once_with("spottings")
        mock_db.collection.return_value.document.assert_called_once_with("U_SPECIFIC")


class TestSetUserCount:
    def test_doc_exists_calls_update(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U123", "spotting_count", 10)
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 10})
        mock_doc_ref.set.assert_not_called()

    def test_doc_missing_calls_set_with_zeroed_defaults(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U_NEW", "spotted_count", 4)
        mock_doc_ref.set.assert_called_once_with({"spotting_count": 0, "spotted_count": 4})
        mock_doc_ref.update.assert_not_called()

    def test_set_spotted_count_on_existing_doc(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U123", "spotted_count", 7)
        mock_doc_ref.update.assert_called_once_with({"spotted_count": 7})

    def test_set_zero_value_on_new_doc(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U_NEW", "spotting_count", 0)
        mock_doc_ref.set.assert_called_once_with({"spotting_count": 0, "spotted_count": 0})


# ===========================================================================
# 8. _run_nightly_count
# ===========================================================================

class TestRunNightlyCount:
    def test_no_channel_id_returns_empty_without_api_call(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", ""):
            result = _run_nightly_count(mock_client)
        assert result == ([], [])
        mock_client.conversations_history.assert_not_called()

    def test_normal_flow_computes_correct_counts(self):
        messages = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UC>", "ts": "2000.0"},
            {"user": "UB", "text": "<@UA>", "ts": "3000.0"},
        ]
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = MagicMock()

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_nightly_count(mock_client)

        spotting_dict = dict(spotting_lb)
        spotted_dict = dict(spotted_lb)
        assert spotting_dict["UA"] == 2
        assert spotting_dict["UB"] == 1
        assert spotted_dict["UB"] == 1
        assert spotted_dict["UC"] == 1
        assert spotted_dict["UA"] == 1

    def test_firebase_set_called_once_per_unique_user(self):
        messages = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
        ]
        mock_client = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            _run_nightly_count(mock_client)

        # UA (spotter) + UB (spotted) = 2 unique users → 2 set() calls
        assert mock_doc_ref.set.call_count == 2

    def test_leaderboards_sorted_descending_by_count(self):
        # UA spots UD twice (>30s apart), UB and UC each once
        messages = [
            {"user": "UA", "text": "<@UD>", "ts": "1000.0"},
            {"user": "UB", "text": "<@UD>", "ts": "2000.0"},
            {"user": "UC", "text": "<@UD>", "ts": "3000.0"},
            {"user": "UA", "text": "<@UD>", "ts": "4000.0"},  # 3000s after first ✓
        ]
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = MagicMock()

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_nightly_count(mock_client)

        # UA has 2 spottings → must appear first
        assert spotting_lb[0][0] == "UA"
        assert spotting_lb[0][1] == 2
        # UD is the only spotted person, appearing in all 4 messages (3 unique after cooldown)
        assert spotted_lb[0][0] == "UD"

    def test_empty_channel_returns_empty_leaderboards(self):
        mock_client = MagicMock()
        mock_db = MagicMock()

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=[]), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_nightly_count(mock_client)

        assert spotting_lb == []
        assert spotted_lb == []


# ===========================================================================
# 9. _post_leaderboard
# ===========================================================================

class TestPostLeaderboard:
    def test_no_channel_id_no_slack_call(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", ""):
            _post_leaderboard(mock_client, [("UA", 3)], [("UB", 2)])
        mock_client.chat_postMessage.assert_not_called()

    def test_posts_to_correct_channel(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOTTINGS"):
            _post_leaderboard(mock_client, [("UA", 5)], [("UB", 3)])
        mock_client.chat_postMessage.assert_called_once()
        kwargs = mock_client.chat_postMessage.call_args[1]
        assert kwargs["channel"] == "C_SPOTTINGS"

    def test_message_includes_blocks(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOTTINGS"):
            _post_leaderboard(mock_client, [("UA", 5)], [])
        kwargs = mock_client.chat_postMessage.call_args[1]
        assert "blocks" in kwargs
        assert isinstance(kwargs["blocks"], list)
        assert len(kwargs["blocks"]) > 0

    def test_empty_leaderboards_still_posts(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOTTINGS"):
            _post_leaderboard(mock_client, [], [])
        mock_client.chat_postMessage.assert_called_once()


# ===========================================================================
# 10. Admin commands: /edit-spotting and /edit-spotted
# ===========================================================================

ADMIN_ID = next(iter(ADMIN_USER_IDS))  # any admin
NON_ADMIN_ID = "U_NOT_AN_ADMIN_000"


class TestAdminCommands:
    def setup_method(self):
        self.app = _make_registered_app()

    def _call_command(self, command, user_id, trigger_id="T123", channel_id="C123"):
        ack = MagicMock()
        body = {"user_id": user_id, "trigger_id": trigger_id, "channel_id": channel_id}
        client = MagicMock()
        logger = MagicMock()
        self.app.commands[command](ack=ack, body=body, client=client, logger=logger)
        return ack, client

    def test_edit_spotting_admin_opens_modal(self):
        ack, client = self._call_command("/edit-spotting", ADMIN_ID)
        ack.assert_called_once()
        client.views_open.assert_called_once()

    def test_edit_spotting_non_admin_blocked(self):
        ack, client = self._call_command("/edit-spotting", NON_ADMIN_ID)
        ack.assert_called_once()
        client.views_open.assert_not_called()
        client.chat_postEphemeral.assert_called_once()
        ephemeral_text = client.chat_postEphemeral.call_args[1]["text"]
        assert "permission" in ephemeral_text.lower()

    def test_edit_spotted_admin_opens_modal(self):
        ack, client = self._call_command("/edit-spotted", ADMIN_ID)
        ack.assert_called_once()
        client.views_open.assert_called_once()

    def test_edit_spotted_non_admin_blocked(self):
        ack, client = self._call_command("/edit-spotted", NON_ADMIN_ID)
        ack.assert_called_once()
        client.views_open.assert_not_called()
        client.chat_postEphemeral.assert_called_once()

    def test_edit_spotting_modal_has_correct_callback_id(self):
        ack, client = self._call_command("/edit-spotting", ADMIN_ID)
        view_arg = client.views_open.call_args[1]["view"]
        assert view_arg["callback_id"] == "spottings_edit_modal"

    def test_edit_spotting_modal_title_contains_spotting(self):
        ack, client = self._call_command("/edit-spotting", ADMIN_ID)
        view_arg = client.views_open.call_args[1]["view"]
        assert "Spotting" in view_arg["title"]["text"]

    def test_edit_spotted_modal_title_contains_spotted(self):
        ack, client = self._call_command("/edit-spotted", ADMIN_ID)
        view_arg = client.views_open.call_args[1]["view"]
        assert "Spotted" in view_arg["title"]["text"]

    def test_missing_trigger_id_does_not_crash_or_open_modal(self):
        ack = MagicMock()
        body = {"user_id": ADMIN_ID, "channel_id": "C123"}  # no trigger_id
        client = MagicMock()
        logger = MagicMock()
        # Must not raise
        self.app.commands["/edit-spotting"](ack=ack, body=body, client=client, logger=logger)
        ack.assert_called_once()
        client.views_open.assert_not_called()  # missing trigger_id → early return

    def test_all_four_admin_ids_can_open_modal(self):
        for admin_id in ADMIN_USER_IDS:
            app = _make_registered_app()
            ack = MagicMock()
            body = {"user_id": admin_id, "trigger_id": "T999", "channel_id": "C123"}
            client = MagicMock()
            app.commands["/edit-spotting"](ack=ack, body=body, client=client, logger=MagicMock())
            client.views_open.assert_called_once(), f"Admin {admin_id} could not open modal"

    def test_both_commands_registered(self):
        assert "/edit-spotting" in self.app.commands
        assert "/edit-spotted" in self.app.commands


# ===========================================================================
# 11. handle_spottings_select action handler
# ===========================================================================

class TestHandleSpottingsSelect:
    def setup_method(self):
        self.app = _make_registered_app()

    def _make_body(self, action_type, selected_user=None, selected_type_value=None,
                   state_user=None, state_type_value=None):
        """Build a realistic action body dict."""
        action = {"type": action_type}
        if action_type == "users_select" and selected_user:
            action["selected_user"] = selected_user
            action["action_id"] = "spottings_user_select"
        if action_type == "static_select" and selected_type_value:
            action["selected_option"] = {"value": selected_type_value}
            action["action_id"] = "spottings_type_select"

        state_values = {}
        if state_user:
            state_values["user_block"] = {
                "spottings_user_select": {"selected_user": state_user}
            }
        if state_type_value:
            state_values["type_block"] = {
                "spottings_type_select": {"selected_option": {"value": state_type_value}}
            }

        return {
            "actions": [action],
            "view": {
                "id": "V_MODAL",
                "hash": "h123",
                "callback_id": "spottings_edit_modal",
                "title": {"type": "plain_text", "text": "Edit Spotting Count"},
                "state": {"values": state_values},
            },
        }

    def test_user_select_populates_spotting_count(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 7, "spotted_count": 2})
        body = self._make_body(
            "users_select", selected_user="U_TARGET", state_type_value="spotting"
        )
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        ack.assert_called_once()
        client.views_update.assert_called_once()
        updated_view = client.views_update.call_args[1]["view"]
        count_block = next(
            b for b in updated_view["blocks"] if b.get("block_id") == "count_block"
        )
        assert count_block["element"]["initial_value"] == "7"

    def test_type_select_populates_spotted_count(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 3, "spotted_count": 9})
        body = self._make_body(
            "static_select", selected_type_value="spotted", state_user="U_TARGET"
        )
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_type_select"](ack=ack, body=body, client=client)

        ack.assert_called_once()
        updated_view = client.views_update.call_args[1]["view"]
        count_block = next(
            b for b in updated_view["blocks"] if b.get("block_id") == "count_block"
        )
        assert count_block["element"]["initial_value"] == "9"

    def test_no_selected_user_returns_early_no_firebase_call(self):
        body = self._make_body("users_select", state_type_value="spotting")  # no selected_user
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore") as mock_get_fs:
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        mock_get_fs.assert_not_called()
        client.views_update.assert_not_called()

    def test_no_view_in_body_returns_early(self):
        ack = MagicMock()
        client = MagicMock()
        body = {"actions": [{"type": "users_select", "selected_user": "U1"}]}  # no "view"

        with patch("features.spottings._get_firestore") as mock_get_fs:
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        mock_get_fs.assert_not_called()
        client.views_update.assert_not_called()

    def test_updated_view_preserves_selected_user_in_user_block(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 4, "spotted_count": 1})
        body = self._make_body(
            "users_select", selected_user="U_TARGET", state_type_value="spotting"
        )
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        updated_view = client.views_update.call_args[1]["view"]
        user_block = next(
            b for b in updated_view["blocks"] if b.get("block_id") == "user_block"
        )
        assert user_block["element"]["initial_user"] == "U_TARGET"

    def test_both_action_ids_are_registered(self):
        assert "spottings_user_select" in self.app.actions
        assert "spottings_type_select" in self.app.actions

    def test_user_select_action_acknowledges(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 0, "spotted_count": 0})
        body = self._make_body(
            "users_select", selected_user="U_TARGET", state_type_value="spotting"
        )
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        ack.assert_called_once()


# ===========================================================================
# 12. handle_spottings_edit_submit view submission
# ===========================================================================

class TestHandleSpottingsEditSubmit:
    def setup_method(self):
        self.app = _make_registered_app()

    def _make_body_and_view(self, user_id, target_user, count_type_value, count_raw,
                             channel="C_CH"):
        body = {
            "user": {"id": user_id},
            "channel": channel,
        }
        type_opt = {"value": count_type_value} if count_type_value else None
        view = {
            "state": {
                "values": {
                    "user_block": {
                        "spottings_user_select": {"selected_user": target_user}
                    },
                    "type_block": {
                        "spottings_type_select": {"selected_option": type_opt}
                    },
                    "count_block": {
                        "spottings_count_input": {"value": count_raw}
                    },
                }
            }
        }
        return body, view

    def _call_submit(self, user_id, target_user, count_type_value, count_raw, channel="C_CH"):
        body, view = self._make_body_and_view(
            user_id, target_user, count_type_value, count_raw, channel
        )
        ack = MagicMock()
        client = MagicMock()
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)

        return ack, client, mock_doc_ref

    def test_admin_valid_positive_count_updates_spotting(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotting", "5")
        ack.assert_called_once()
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 5})

    def test_admin_updates_spotted_count_field(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotted", "8")
        mock_doc_ref.update.assert_called_once_with({"spotted_count": 8})

    def test_admin_receives_ephemeral_confirmation(self):
        ack, client, _ = self._call_submit(
            ADMIN_ID, "U_TARGET", "spotting", "5", channel="C_CHAN"
        )
        client.chat_postEphemeral.assert_called_once()
        kwargs = client.chat_postEphemeral.call_args[1]
        assert kwargs["channel"] == "C_CHAN"
        assert "U_TARGET" in kwargs["text"]
        assert "5" in kwargs["text"]

    def test_negative_count_clamped_to_zero(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotting", "-3")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_non_numeric_count_defaults_to_zero(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotting", "abc")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_whitespace_only_count_defaults_to_zero(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotting", "   ")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_non_admin_does_not_update_firebase(self):
        body, view = self._make_body_and_view(NON_ADMIN_ID, "U_TARGET", "spotting", "5")
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore") as mock_get_fs:
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)

        mock_get_fs.assert_not_called()
        client.chat_postEphemeral.assert_not_called()

    def test_missing_target_user_returns_early(self):
        body = {"user": {"id": ADMIN_ID}, "channel": "C_CH"}
        view = {
            "state": {
                "values": {
                    "user_block": {"spottings_user_select": {"selected_user": None}},
                    "type_block": {
                        "spottings_type_select": {"selected_option": {"value": "spotting"}}
                    },
                    "count_block": {"spottings_count_input": {"value": "5"}},
                }
            }
        }
        ack = MagicMock()
        client = MagicMock()

        with patch("features.spottings._get_firestore") as mock_get_fs:
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)

        mock_get_fs.assert_not_called()

    def test_no_channel_in_body_no_crash_still_updates_firebase(self):
        body = {"user": {"id": ADMIN_ID}}  # no "channel" or "container"
        view = {
            "state": {
                "values": {
                    "user_block": {"spottings_user_select": {"selected_user": "U_TARGET"}},
                    "type_block": {
                        "spottings_type_select": {"selected_option": {"value": "spotting"}}
                    },
                    "count_block": {"spottings_count_input": {"value": "3"}},
                }
            }
        }
        ack = MagicMock()
        client = MagicMock()
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)

        with patch("features.spottings._get_firestore", return_value=mock_db):
            # Must not raise
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)

        mock_doc_ref.update.assert_called_once_with({"spotting_count": 3})
        client.chat_postEphemeral.assert_not_called()

    def test_zero_count_is_valid(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotting", "0")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_large_count_is_accepted(self):
        ack, client, mock_doc_ref = self._call_submit(ADMIN_ID, "U_TARGET", "spotted", "9999")
        mock_doc_ref.update.assert_called_once_with({"spotted_count": 9999})

    def test_submit_view_is_registered(self):
        assert "spottings_edit_modal" in self.app.views
