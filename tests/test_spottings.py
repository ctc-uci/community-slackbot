"""
Rigorous tests for features/spottings.py.

Covers every behavioral requirement without real Firebase or Slack connections.
All external I/O is mocked.
"""

import json
import sys
import os
import threading
from datetime import datetime, timedelta
from unittest.mock import MagicMock, patch, call

import pytest
import pytz

# ---------------------------------------------------------------------------
# Project root on path so "features" and "firebase_client" resolve.
# Direct import keeps features.spottings in sys.modules, which is required
# for subsequent patch() calls to target the correct module globals.
# ---------------------------------------------------------------------------
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from features.spottings import (  # noqa: E402
    _extract_mentions,
    _apply_cooldown,
    _compute_counts_from_messages,
    _build_leaderboard_blocks,
    _build_leaderboard_from_firebase,
    _seconds_until_next_run,
    _fetch_channel_messages,
    _get_user_counts,
    _set_user_count,
    _get_last_processed_ts,
    _set_last_processed_ts,
    _run_incremental_count,
    _run_full_recount,
    _post_leaderboard,
    register_spottings_handlers,
    ADMIN_USER_IDS,
    FIRESTORE_COLLECTION,
    SPOTTINGS_META_COLLECTION,
    SPOTTINGS_META_DOC,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class CapturingApp:
    """Minimal mock of slack_bolt.App that records registered handlers."""
    def __init__(self):
        self.commands = {}
        self.actions = {}
        self.views = {}

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
    app = CapturingApp()
    with patch("threading.Thread"):
        register_spottings_handlers(app)
    return app


def _make_firestore_mock(doc_data=None, doc_exists=True):
    mock_doc = MagicMock()
    mock_doc.exists = doc_exists
    if doc_data is not None:
        mock_doc.to_dict.return_value = doc_data
    mock_doc_ref = MagicMock()
    mock_doc_ref.get.return_value = mock_doc
    mock_db = MagicMock()
    mock_db.collection.return_value.document.return_value = mock_doc_ref
    return mock_db, mock_doc_ref


ADMIN_ID = next(iter(ADMIN_USER_IDS))
NON_ADMIN_ID = "U_NOT_ADMIN_000"


# ===========================================================================
# 1. _extract_mentions
# ===========================================================================

class TestExtractMentions:
    def test_single_mention(self):
        assert _extract_mentions("<@U123>") == {"U123"}

    def test_mention_with_display_name(self):
        assert _extract_mentions("<@U123|john>") == {"U123"}

    def test_multiple_distinct(self):
        assert _extract_mentions("<@UA> and <@UB>") == {"UA", "UB"}

    def test_duplicate_deduped_by_set(self):
        assert _extract_mentions("<@UA> <@UA>") == {"UA"}

    def test_no_mentions(self):
        assert _extract_mentions("hello") == set()

    def test_empty_string(self):
        assert _extract_mentions("") == set()

    def test_none_returns_empty(self):
        assert _extract_mentions(None) == set()


# ===========================================================================
# 2. _apply_cooldown
# ===========================================================================

class TestApplyCooldown:
    def test_empty(self):
        assert _apply_cooldown([]) == []

    def test_single_pair(self):
        assert _apply_cooldown([("UA", "UB", 1000.0)]) == [("UA", "UB")]

    def test_within_30s_deduped(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1020.0)]
        assert len(_apply_cooldown(pairs)) == 1

    def test_exactly_30s_counts_again(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1030.0)]
        assert len(_apply_cooldown(pairs)) == 2

    def test_31s_apart_counts_twice(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1031.0)]
        assert len(_apply_cooldown(pairs)) == 2

    def test_different_pairs_within_30s_both_counted(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UC", 1005.0)]
        result = _apply_cooldown(pairs)
        assert len(result) == 2

    def test_out_of_order_sorted_first(self):
        pairs = [("UA", "UB", 1020.0), ("UA", "UB", 1000.0)]
        assert len(_apply_cooldown(pairs)) == 1  # 20s apart → deduped

    def test_same_timestamp_deduped(self):
        pairs = [("UA", "UB", 1000.0), ("UA", "UB", 1000.0)]
        assert len(_apply_cooldown(pairs)) == 1


# ===========================================================================
# 3. _compute_counts_from_messages
# ===========================================================================

class TestComputeCountsFromMessages:
    def test_empty(self):
        assert _compute_counts_from_messages([]) == ({}, {})

    def test_no_user_skipped(self):
        msgs = [{"text": "<@UB>", "ts": "1000.0"}]
        assert _compute_counts_from_messages(msgs) == ({}, {})

    def test_self_mention_excluded(self):
        msgs = [{"user": "UA", "text": "<@UA>", "ts": "1000.0"}]
        assert _compute_counts_from_messages(msgs) == ({}, {})

    def test_basic_spotting(self):
        msgs = [{"user": "UA", "text": "<@UB>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}

    def test_duplicate_tag_in_message_counts_once(self):
        msgs = [{"user": "UA", "text": "<@UB> <@UB>", "ts": "1000.0"}]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}
        assert spotted == {"UB": 1}

    def test_two_messages_within_30s_counts_once(self):
        msgs = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UB>", "ts": "1020.0"},
        ]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 1}

    def test_two_messages_after_30s_counts_twice(self):
        msgs = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UB>", "ts": "1031.0"},
        ]
        spotting, spotted = _compute_counts_from_messages(msgs)
        assert spotting == {"UA": 2}


# ===========================================================================
# 4. _build_leaderboard_blocks
# ===========================================================================

class TestBuildLeaderboardBlocks:
    def _texts(self, blocks):
        return [b["text"]["text"] for b in blocks if b.get("type") == "section"]

    def test_both_empty(self):
        blocks = _build_leaderboard_blocks([], [])
        full = "\n".join(self._texts(blocks))
        assert "_No spottings yet!_" in full
        assert "_No one spotted yet!_" in full

    def test_populated_correct_rank_format(self):
        blocks = _build_leaderboard_blocks([("UA", 5), ("UB", 3)], [("UC", 7)])
        full = "\n".join(self._texts(blocks))
        assert "1. <@UA>" in full
        assert "2. <@UB>" in full
        assert "1. <@UC>" in full

    def test_top_10_cap(self):
        lb = [(f"U{i:03d}", 100 - i) for i in range(15)]
        blocks = _build_leaderboard_blocks(lb, [])
        full = "\n".join(self._texts(blocks))
        assert "10. " in full
        assert "11. " not in full

    def test_has_header_and_divider(self):
        blocks = _build_leaderboard_blocks([], [])
        types = [b["type"] for b in blocks]
        assert "header" in types
        assert "divider" in types


# ===========================================================================
# 5. _seconds_until_next_run
# ===========================================================================

class TestSecondsUntilNextRun:
    TZ = pytz.timezone("America/Los_Angeles")

    def _run(self, hour, minute, target_hour, target_minute):
        now_dt = self.TZ.localize(datetime(2024, 6, 15, hour, minute, 0))
        class FakeDatetime:
            @staticmethod
            def now(tz): return now_dt
        with patch("features.spottings.datetime", FakeDatetime):
            return _seconds_until_next_run(target_hour, target_minute)

    def test_future_target(self):
        result = self._run(10, 0, 23, 59)
        assert abs(result - ((23 - 10) * 3600 + 59 * 60)) < 2

    def test_past_target_schedules_tomorrow(self):
        now_dt = self.TZ.localize(datetime(2024, 6, 15, 23, 59, 30))
        class FakeDatetime:
            @staticmethod
            def now(tz): return now_dt
        with patch("features.spottings.datetime", FakeDatetime):
            result = _seconds_until_next_run(23, 59)
        assert 86300 < result < 86400

    def test_always_positive(self):
        for hour in [0, 8, 16, 23]:
            assert self._run(hour, 30, 23, 59) > 0


# ===========================================================================
# 6. _fetch_channel_messages
# ===========================================================================

class TestFetchChannelMessages:
    def _msg(self, user="U1", text="hi", ts="1000.0", **kw):
        return {"user": user, "text": text, "ts": ts, **kw}

    def test_single_page(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [self._msg()], "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C1")
        assert len(result) == 1

    def test_bot_excluded(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [self._msg(), self._msg(bot_id="B1")], "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C1")
        assert len(result) == 1

    def test_subtypes_excluded(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [
                self._msg(subtype="channel_join"),
                self._msg(subtype="channel_leave"),
                self._msg(subtype="bot_message"),
                self._msg(user="U4"),
            ], "has_more": False,
        }
        result = _fetch_channel_messages(mock_client, "C1")
        assert len(result) == 1
        assert result[0]["user"] == "U4"

    def test_pagination(self):
        mock_client = MagicMock()
        mock_client.conversations_history.side_effect = [
            {"messages": [self._msg(user="U1")], "has_more": True,
             "response_metadata": {"next_cursor": "cur_abc"}},
            {"messages": [self._msg(user="U2")], "has_more": False},
        ]
        result = _fetch_channel_messages(mock_client, "C1")
        assert len(result) == 2
        second_kwargs = mock_client.conversations_history.call_args_list[1][1]
        assert second_kwargs["cursor"] == "cur_abc"

    def test_oldest_param_passed(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [], "has_more": False,
        }
        _fetch_channel_messages(mock_client, "C1", oldest=1234567890.5)
        call_kwargs = mock_client.conversations_history.call_args[1]
        assert call_kwargs["oldest"] == "1234567890.5"

    def test_oldest_zero_not_passed(self):
        mock_client = MagicMock()
        mock_client.conversations_history.return_value = {
            "messages": [], "has_more": False,
        }
        _fetch_channel_messages(mock_client, "C1", oldest=0.0)
        call_kwargs = mock_client.conversations_history.call_args[1]
        assert "oldest" not in call_kwargs


# ===========================================================================
# 7. _get_user_counts / _set_user_count
# ===========================================================================

class TestGetUserCounts:
    def test_doc_exists(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 5, "spotted_count": 3})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U1")
        assert result == {"spotting_count": 5, "spotted_count": 3}

    def test_doc_missing_returns_zeros(self):
        mock_db, _ = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U_NEW")
        assert result == {"spotting_count": 0, "spotted_count": 0}

    def test_partial_doc_defaults_to_zero(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 7})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_user_counts("U1")
        assert result["spotted_count"] == 0


class TestSetUserCount:
    def test_existing_doc_calls_update(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U1", "spotting_count", 10)
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 10})

    def test_missing_doc_calls_set_with_defaults(self):
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_user_count("U_NEW", "spotted_count", 4)
        mock_doc_ref.set.assert_called_once_with({"spotting_count": 0, "spotted_count": 4})


# ===========================================================================
# 8. _get_last_processed_ts / _set_last_processed_ts
# ===========================================================================

class TestLastProcessedTs:
    def test_get_returns_stored_value(self):
        mock_db, _ = _make_firestore_mock({"last_processed_ts": 1717200000.5})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_last_processed_ts()
        assert result == 1717200000.5

    def test_get_missing_doc_returns_zero(self):
        mock_db, _ = _make_firestore_mock(doc_exists=False)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            result = _get_last_processed_ts()
        assert result == 0.0

    def test_get_queries_correct_collection_and_doc(self):
        mock_db, _ = _make_firestore_mock({"last_processed_ts": 0.0})
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _get_last_processed_ts()
        mock_db.collection.assert_called_with(SPOTTINGS_META_COLLECTION)
        mock_db.collection.return_value.document.assert_called_with(SPOTTINGS_META_DOC)

    def test_set_writes_with_merge(self):
        mock_db = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref
        with patch("features.spottings._get_firestore", return_value=mock_db):
            _set_last_processed_ts(9999999.0)
        mock_doc_ref.set.assert_called_once_with(
            {"last_processed_ts": 9999999.0}, merge=True
        )


# ===========================================================================
# 9. _build_leaderboard_from_firebase
# ===========================================================================

class TestBuildLeaderboardFromFirebase:
    def test_streams_collection_and_sorts(self):
        doc_a = MagicMock(); doc_a.id = "UA"; doc_a.to_dict.return_value = {"spotting_count": 3, "spotted_count": 1}
        doc_b = MagicMock(); doc_b.id = "UB"; doc_b.to_dict.return_value = {"spotting_count": 7, "spotted_count": 2}
        mock_db = MagicMock()
        mock_db.collection.return_value.stream.return_value = [doc_a, doc_b]

        with patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _build_leaderboard_from_firebase()

        # UB has more spottings → first
        assert spotting_lb[0] == ("UB", 7)
        assert spotting_lb[1] == ("UA", 3)

    def test_empty_collection_returns_empty_lists(self):
        mock_db = MagicMock()
        mock_db.collection.return_value.stream.return_value = []
        with patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _build_leaderboard_from_firebase()
        assert spotting_lb == []
        assert spotted_lb == []


# ===========================================================================
# 10. _run_incremental_count
# ===========================================================================

class TestRunIncrementalCount:
    def test_no_channel_id_returns_empty(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", ""):
            result = _run_incremental_count(mock_client)
        assert result == ([], [])

    def test_first_run_uses_plain_set(self):
        """last_ts == 0 → bootstrap path → uses set() not Increment"""
        messages = [{"user": "UA", "text": "<@UB>", "ts": "1000.0"}]
        mock_client = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref
        # stream() for leaderboard
        mock_db.collection.return_value.stream.return_value = []

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._get_last_processed_ts", return_value=0.0), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._set_last_processed_ts") as mock_set_ts, \
             patch("features.spottings._get_firestore", return_value=mock_db):
            _run_incremental_count(mock_client)

        # set() called (not set with merge=True on a sentinel)
        mock_doc_ref.set.assert_called()
        # Verify the call did NOT use firestore.Increment (it should be plain ints)
        set_call_args = mock_doc_ref.set.call_args
        data = set_call_args[0][0]  # first positional arg
        assert isinstance(data["spotting_count"], int) or isinstance(data["spotted_count"], int)
        mock_set_ts.assert_called_once_with(1000.0)

    def test_incremental_run_uses_increment_sentinel(self):
        """last_ts > 0 → incremental path → uses firestore.Increment"""
        messages = [{"user": "UA", "text": "<@UB>", "ts": "2000.0"}]
        mock_client = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref
        mock_db.collection.return_value.stream.return_value = []

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._get_last_processed_ts", return_value=1000.0), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._set_last_processed_ts") as mock_set_ts, \
             patch("features.spottings._get_firestore", return_value=mock_db):
            _run_incremental_count(mock_client)

        # set() called with merge=True (incremental path)
        mock_doc_ref.set.assert_called()
        call_args, call_kwargs = mock_doc_ref.set.call_args
        assert call_kwargs.get("merge") is True
        mock_set_ts.assert_called_once_with(2000.0)

    def test_oldest_param_passed_with_epsilon(self):
        """Verifies fetch is called with oldest = last_ts + 1e-6"""
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.stream.return_value = []

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._get_last_processed_ts", return_value=1000.0), \
             patch("features.spottings._fetch_channel_messages", return_value=[]) as mock_fetch, \
             patch("features.spottings._get_firestore", return_value=mock_db):
            _run_incremental_count(mock_client)

        mock_fetch.assert_called_once()
        _, kwargs = mock_fetch.call_args
        assert abs(kwargs["oldest"] - 1000.000001) < 1e-9

    def test_no_new_messages_returns_firebase_leaderboard(self):
        mock_client = MagicMock()
        mock_db = MagicMock()
        doc = MagicMock(); doc.id = "UA"; doc.to_dict.return_value = {"spotting_count": 5, "spotted_count": 2}
        mock_db.collection.return_value.stream.return_value = [doc]

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._get_last_processed_ts", return_value=1000.0), \
             patch("features.spottings._fetch_channel_messages", return_value=[]), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_incremental_count(mock_client)

        assert spotting_lb == [("UA", 5)]
        assert spotted_lb == [("UA", 2)]


# ===========================================================================
# 11. _run_full_recount
# ===========================================================================

class TestRunFullRecount:
    def test_no_channel_returns_empty(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", ""):
            result = _run_full_recount(mock_client)
        assert result == ([], [])

    def test_overwrites_firebase_and_updates_ts(self):
        messages = [
            {"user": "UA", "text": "<@UB>", "ts": "1000.0"},
            {"user": "UA", "text": "<@UC>", "ts": "2000.0"},
        ]
        mock_client = MagicMock()
        mock_doc_ref = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = mock_doc_ref

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._set_last_processed_ts") as mock_set_ts, \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_full_recount(mock_client)

        # set() called without merge (overwrite) for each unique user
        assert mock_doc_ref.set.call_count == 3  # UA, UB, UC
        mock_set_ts.assert_called_once_with(2000.0)

    def test_no_oldest_param_passed(self):
        """Full recount must fetch all history with no oldest filter."""
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = MagicMock()

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=[]) as mock_fetch, \
             patch("features.spottings._set_last_processed_ts"), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            _run_full_recount(mock_client)

        _, kwargs = mock_fetch.call_args
        assert kwargs.get("oldest", 0.0) == 0.0

    def test_returns_sorted_leaderboards(self):
        messages = [
            {"user": "UA", "text": "<@UD>", "ts": "1000.0"},
            {"user": "UB", "text": "<@UD>", "ts": "2000.0"},
            {"user": "UA", "text": "<@UD>", "ts": "4000.0"},  # UA spots twice (>30s)
        ]
        mock_client = MagicMock()
        mock_db = MagicMock()
        mock_db.collection.return_value.document.return_value = MagicMock()

        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"), \
             patch("features.spottings._fetch_channel_messages", return_value=messages), \
             patch("features.spottings._set_last_processed_ts"), \
             patch("features.spottings._get_firestore", return_value=mock_db):
            spotting_lb, spotted_lb = _run_full_recount(mock_client)

        assert spotting_lb[0][0] == "UA"
        assert spotting_lb[0][1] == 2


# ===========================================================================
# 12. _post_leaderboard
# ===========================================================================

class TestPostLeaderboard:
    def test_no_channel_no_call(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", ""):
            _post_leaderboard(mock_client, [], [])
        mock_client.chat_postMessage.assert_not_called()

    def test_posts_to_correct_channel(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"):
            _post_leaderboard(mock_client, [("UA", 3)], [("UB", 2)])
        assert mock_client.chat_postMessage.call_args[1]["channel"] == "C_SPOT"

    def test_empty_leaderboards_still_posts(self):
        mock_client = MagicMock()
        with patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"):
            _post_leaderboard(mock_client, [], [])
        mock_client.chat_postMessage.assert_called_once()


# ===========================================================================
# 13. Admin commands: /edit-spotting, /edit-spotted, /recount-spottings
# ===========================================================================

class TestAdminCommands:
    def setup_method(self):
        self.app = _make_registered_app()

    def _call(self, command, user_id, trigger_id="T1", channel_id="C1"):
        ack = MagicMock()
        body = {"user_id": user_id, "trigger_id": trigger_id, "channel_id": channel_id}
        client = MagicMock()
        self.app.commands[command](ack=ack, body=body, client=client, logger=MagicMock())
        return ack, client

    def test_edit_spotting_admin_opens_modal(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        ack.assert_called_once()
        client.views_open.assert_called_once()

    def test_edit_spotting_non_admin_blocked(self):
        ack, client = self._call("/edit-spotting", NON_ADMIN_ID)
        client.views_open.assert_not_called()
        client.chat_postEphemeral.assert_called_once()

    def test_edit_spotted_admin_opens_modal(self):
        ack, client = self._call("/edit-spotted", ADMIN_ID)
        client.views_open.assert_called_once()

    def test_modal_has_callback_id(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        assert view["callback_id"] == "spottings_edit_modal"

    def test_modal_has_private_metadata_with_default_type(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        meta = json.loads(view["private_metadata"])
        assert meta["default_type"] == "spotting"

    def test_edit_spotted_modal_private_metadata_spotted(self):
        ack, client = self._call("/edit-spotted", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        meta = json.loads(view["private_metadata"])
        assert meta["default_type"] == "spotted"

    def test_modal_has_current_count_display_block(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        block_ids = [b.get("block_id") for b in view["blocks"]]
        assert "current_count_display" in block_ids

    def test_current_count_display_is_section_not_input(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        display = next(b for b in view["blocks"] if b.get("block_id") == "current_count_display")
        assert display["type"] == "section"

    def test_all_admin_ids_can_open_modal(self):
        for admin_id in ADMIN_USER_IDS:
            app = _make_registered_app()
            ack = MagicMock(); client = MagicMock()
            app.commands["/edit-spotting"](
                ack=ack, body={"user_id": admin_id, "trigger_id": "T1", "channel_id": "C1"},
                client=client, logger=MagicMock()
            )
            client.views_open.assert_called_once()

    def test_recount_admin_starts_thread(self):
        ack, client = self._call("/recount-spottings", ADMIN_ID)
        ack.assert_called_once()

    def test_recount_non_admin_blocked(self):
        ack, client = self._call("/recount-spottings", NON_ADMIN_ID)
        client.chat_postEphemeral.assert_called_once()

    def test_all_three_commands_registered(self):
        assert "/edit-spotting" in self.app.commands
        assert "/edit-spotted" in self.app.commands
        assert "/recount-spottings" in self.app.commands

    def test_user_block_has_dispatch_action(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        user_block = next(b for b in view["blocks"] if b.get("block_id") == "user_block")
        assert user_block.get("dispatch_action") is True

    def test_type_block_has_dispatch_action(self):
        ack, client = self._call("/edit-spotting", ADMIN_ID)
        view = client.views_open.call_args[1]["view"]
        type_block = next(b for b in view["blocks"] if b.get("block_id") == "type_block")
        assert type_block.get("dispatch_action") is True


# ===========================================================================
# 14. handle_spottings_select action handler
# ===========================================================================

class TestHandleSpottingsSelect:
    def setup_method(self):
        self.app = _make_registered_app()

    def _make_body(self, action_type, selected_user=None, selected_type=None,
                   state_user=None, state_type=None, default_type="spotting"):
        action = {"type": action_type}
        if action_type == "users_select" and selected_user:
            action["selected_user"] = selected_user
        if action_type == "static_select" and selected_type:
            action["selected_option"] = {"value": selected_type}

        state_values = {}
        if state_user:
            state_values["user_block"] = {"spottings_user_select": {"selected_user": state_user}}
        if state_type:
            state_values["type_block"] = {"spottings_type_select": {"selected_option": {"value": state_type}}}

        return {
            "actions": [action],
            "view": {
                "id": "V1", "hash": "h1",
                "callback_id": "spottings_edit_modal",
                "title": {"type": "plain_text", "text": "Edit"},
                "private_metadata": json.dumps({"default_type": default_type}),
                "state": {"values": state_values},
            },
        }

    def test_user_select_populates_count(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 7, "spotted_count": 2})
        body = self._make_body("users_select", selected_user="U_T", state_type="spotting")
        ack = MagicMock(); client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        updated_view = client.views_update.call_args[1]["view"]
        count_block = next(b for b in updated_view["blocks"] if b.get("block_id") == "count_block")
        assert count_block["element"]["initial_value"] == "7"

    def test_current_count_display_populated(self):
        mock_db, _ = _make_firestore_mock({"spotting_count": 7, "spotted_count": 2})
        body = self._make_body("users_select", selected_user="U_T", state_type="spotting")
        ack = MagicMock(); client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        updated_view = client.views_update.call_args[1]["view"]
        display = next(b for b in updated_view["blocks"] if b.get("block_id") == "current_count_display")
        assert "7" in display["text"]["text"]

    def test_type_defaults_from_private_metadata_when_not_in_state(self):
        """Key fix: selecting a user without touching the type dropdown works via private_metadata."""
        mock_db, _ = _make_firestore_mock({"spotting_count": 5, "spotted_count": 9})
        # No state_type → type_block not in state.values
        body = self._make_body("users_select", selected_user="U_T", default_type="spotted")
        ack = MagicMock(); client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        updated_view = client.views_update.call_args[1]["view"]
        count_block = next(b for b in updated_view["blocks"] if b.get("block_id") == "count_block")
        # Should show spotted count (9) since default_type = "spotted"
        assert count_block["element"]["initial_value"] == "9"

    def test_no_selected_user_returns_early(self):
        ack = MagicMock(); client = MagicMock()
        body = self._make_body("users_select")  # no selected_user

        with patch("features.spottings._get_firestore") as mock_fs:
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        mock_fs.assert_not_called()
        client.views_update.assert_not_called()

    def test_no_view_returns_early(self):
        ack = MagicMock(); client = MagicMock()
        body = {"actions": [{"type": "users_select", "selected_user": "U1"}]}

        with patch("features.spottings._get_firestore") as mock_fs:
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        mock_fs.assert_not_called()

    def test_both_action_ids_registered(self):
        assert "spottings_user_select" in self.app.actions
        assert "spottings_type_select" in self.app.actions

    def test_views_update_blocks_have_dispatch_action(self):
        """After a user selection, the updated modal blocks must retain dispatch_action."""
        mock_db, _ = _make_firestore_mock({"spotting_count": 3, "spotted_count": 1})
        body = self._make_body("users_select", selected_user="U_T", state_type="spotting")
        ack = MagicMock(); client = MagicMock()

        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.actions["spottings_user_select"](ack=ack, body=body, client=client)

        updated_view = client.views_update.call_args[1]["view"]
        user_block = next(b for b in updated_view["blocks"] if b.get("block_id") == "user_block")
        type_block = next(b for b in updated_view["blocks"] if b.get("block_id") == "type_block")
        assert user_block.get("dispatch_action") is True
        assert type_block.get("dispatch_action") is True


# ===========================================================================
# 15. handle_spottings_edit_submit
# ===========================================================================

class TestHandleSpottingsEditSubmit:
    def setup_method(self):
        self.app = _make_registered_app()

    def _make_body_view(self, user_id, target_user, count_type, count_raw, channel="C1"):
        body = {"user": {"id": user_id}, "channel": channel}
        type_opt = {"value": count_type} if count_type else None
        view = {"state": {"values": {
            "user_block": {"spottings_user_select": {"selected_user": target_user}},
            "type_block": {"spottings_type_select": {"selected_option": type_opt}},
            "count_block": {"spottings_count_input": {"value": count_raw}},
        }}}
        return body, view

    def _submit(self, user_id, target_user, count_type, count_raw, channel="C1"):
        body, view = self._make_body_view(user_id, target_user, count_type, count_raw, channel)
        ack = MagicMock(); client = MagicMock()
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)
        return ack, client, mock_doc_ref

    def test_valid_count_updates_firebase(self):
        ack, client, mock_doc_ref = self._submit(ADMIN_ID, "U_T", "spotting", "5")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 5})

    def test_spotted_count_correct_field(self):
        ack, client, mock_doc_ref = self._submit(ADMIN_ID, "U_T", "spotted", "8")
        mock_doc_ref.update.assert_called_once_with({"spotted_count": 8})

    def test_ephemeral_confirmation_sent(self):
        ack, client, _ = self._submit(ADMIN_ID, "U_T", "spotting", "5", channel="C_CH")
        client.chat_postEphemeral.assert_called_once()
        assert "U_T" in client.chat_postEphemeral.call_args[1]["text"]

    def test_negative_clamped_to_zero(self):
        ack, client, mock_doc_ref = self._submit(ADMIN_ID, "U_T", "spotting", "-3")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_non_numeric_defaults_to_zero(self):
        ack, client, mock_doc_ref = self._submit(ADMIN_ID, "U_T", "spotting", "abc")
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 0})

    def test_non_admin_blocked(self):
        body, view = self._make_body_view(NON_ADMIN_ID, "U_T", "spotting", "5")
        ack = MagicMock(); client = MagicMock()
        with patch("features.spottings._get_firestore") as mock_fs:
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)
        mock_fs.assert_not_called()

    def test_no_channel_no_crash(self):
        body = {"user": {"id": ADMIN_ID}}
        view = {"state": {"values": {
            "user_block": {"spottings_user_select": {"selected_user": "U_T"}},
            "type_block": {"spottings_type_select": {"selected_option": {"value": "spotting"}}},
            "count_block": {"spottings_count_input": {"value": "3"}},
        }}}
        ack = MagicMock(); client = MagicMock()
        mock_db, mock_doc_ref = _make_firestore_mock(doc_exists=True)
        with patch("features.spottings._get_firestore", return_value=mock_db):
            self.app.views["spottings_edit_modal"](ack=ack, body=body, client=client, view=view)
        mock_doc_ref.update.assert_called_once_with({"spotting_count": 3})
        client.chat_postEphemeral.assert_not_called()

    def test_submit_handler_registered(self):
        assert "spottings_edit_modal" in self.app.views


# ===========================================================================
# 16. Scheduler loop: joins channel before processing
# ===========================================================================

class TestSchedulerLoop:
    def test_joins_channel_before_incremental_count(self):
        """conversations_join is called at startup to ensure bot is in the channel."""
        mock_client_instance = MagicMock()
        with (
            patch("features.spottings.SLACK_BOT_TOKEN", "xoxb-fake", create=True),
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}),
            patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"),
            patch("features.spottings.WebClient", return_value=mock_client_instance),
            patch("features.spottings._run_incremental_count") as mock_run,
            patch("features.spottings.time.sleep", side_effect=StopIteration),
        ):
            from features.spottings import _scheduler_loop
            try:
                _scheduler_loop()
            except StopIteration:
                pass
        mock_client_instance.conversations_join.assert_called_once_with(channel="C_SPOT")

    def test_joins_channel_before_startup_recount(self):
        """conversations_join is called before _run_incremental_count on startup."""
        call_order = []
        mock_client_instance = MagicMock()
        mock_client_instance.conversations_join.side_effect = lambda **kw: call_order.append("join")

        def fake_incremental(client):
            call_order.append("recount")
            return [], []

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}),
            patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"),
            patch("features.spottings.WebClient", return_value=mock_client_instance),
            patch("features.spottings._run_incremental_count", side_effect=fake_incremental),
            patch("features.spottings.time.sleep", side_effect=StopIteration),
        ):
            from features.spottings import _scheduler_loop
            try:
                _scheduler_loop()
            except StopIteration:
                pass
        assert call_order[0] == "join", "conversations_join must be called before _run_incremental_count"
        assert "recount" in call_order

    def test_join_failure_does_not_prevent_recount(self):
        """If conversations_join raises (e.g. already in channel), recount still runs."""
        mock_client_instance = MagicMock()
        mock_client_instance.conversations_join.side_effect = Exception("already_in_channel")

        with (
            patch.dict("os.environ", {"SLACK_BOT_TOKEN": "xoxb-fake"}),
            patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"),
            patch("features.spottings.WebClient", return_value=mock_client_instance),
            patch("features.spottings._run_incremental_count") as mock_run,
            patch("features.spottings.time.sleep", side_effect=StopIteration),
        ):
            from features.spottings import _scheduler_loop
            try:
                _scheduler_loop()
            except StopIteration:
                pass
        mock_run.assert_called_once()

    def test_no_token_exits_early(self):
        """Scheduler exits immediately if SLACK_BOT_TOKEN is not set."""
        with (
            patch.dict("os.environ", {}, clear=True),
            patch("features.spottings.SPOTTINGS_CHANNEL_ID", "C_SPOT"),
            patch("features.spottings.WebClient") as mock_wc,
        ):
            from features.spottings import _scheduler_loop
            _scheduler_loop()
        mock_wc.assert_not_called()
