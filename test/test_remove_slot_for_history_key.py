"""Tests for _remove_slot_for_history_key in handlers.py."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from kiro_crew import session_ledger
from kiro_crew.dashboard.handlers import _remove_slot_for_history_key


def _make_state(slots: dict) -> MagicMock:
    state = MagicMock()
    state._slots = dict(slots)
    state.push_slots_update = MagicMock()
    state.sessions = MagicMock()
    state.sessions.destroy = AsyncMock()
    state.sessions.drop_autocompact_overrides_matching = MagicMock(return_value=0)
    state.remove_chat_pins_for_slots = AsyncMock()
    state.conversation_log = MagicMock()
    state.conversation_log.list_sessions = MagicMock(return_value=[])
    return state


def _make_slot(key: str, running: bool = False) -> MagicMock:
    slot = MagicMock()
    slot.key = key
    slot.running = running
    # A real slot is unbound unless its conversation lives on another session.
    # Left unset, a bare MagicMock hands back a truthy Mock as the session key,
    # so the teardown would target something that is not a key at all.
    slot.linked_session_key = ""
    slot.channel_origin = False
    if running:

        async def _hang():
            await asyncio.sleep(999)

        slot.task = asyncio.ensure_future(_hang())
    else:
        slot.task = None
    return slot


class TestRemoveSlotForHistoryKey:
    @pytest.mark.asyncio
    async def test_exact_key_match(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_stripped_key_match(self):
        slot = _make_slot("chat-1-100")
        state = _make_state({"chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-1-100" not in state._slots

    @pytest.mark.asyncio
    async def test_colon_prefix_stripped(self):
        slot = _make_slot("chat-2-200")
        state = _make_state({"chat-2-200": slot})
        await _remove_slot_for_history_key(state, "dashboard:chat-2-200")
        assert "chat-2-200" not in state._slots

    @pytest.mark.asyncio
    async def test_no_match_is_noop(self):
        state = _make_state({"chat-9-999": _make_slot("chat-9-999")})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "chat-9-999" in state._slots
        state.sessions.destroy.assert_not_called()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deleted_key",
        ("slack:team:direct:42", "slack_team_direct_42"),
        ids=("logical-key", "dashboard-list-stem"),
    )
    async def test_lossy_fold_does_not_remove_foreign_running_slot(
        self, deleted_key: str, monkeypatch: pytest.MonkeyPatch
    ):
        """A channel key and dashboard slot key can fold to one spelling while
        their transcripts remain distinct because the dashboard one is prefixed."""
        direct_purges: list[str] = []
        matching_calls: list[tuple[set[str], set[str]]] = []

        def capture_matching(exact: set[str], folded: set[str], _fold) -> int:
            matching_calls.append((set(exact), set(folded)))
            return 0

        monkeypatch.setattr(session_ledger, "purge", direct_purges.append)
        monkeypatch.setattr(session_ledger, "purge_matching", capture_matching)
        slot = _make_slot("slack_team_direct_42", running=True)
        state = _make_state({slot.key: slot})

        try:
            await _remove_slot_for_history_key(state, deleted_key)

            assert state._slots[slot.key] is slot
            assert not slot.task.done()
            state.sessions.destroy.assert_not_awaited()

            pin_keys = state.remove_chat_pins_for_slots.await_args.args[0]
            assert slot.key not in pin_keys
            assert slot.key not in direct_purges
            assert len(matching_calls) == 1
            ledger_exact, ledger_folded = matching_calls[0]
            assert slot.key not in ledger_exact
            assert slot.key not in ledger_folded

            override_exact, override_folded, _fold = (
                state.sessions.drop_autocompact_overrides_matching.call_args.args
            )
            assert slot.key not in override_exact
            assert f"dashboard:{slot.key}" not in override_exact
            assert slot.key not in override_folded
        finally:
            if not slot.task.done():
                slot.task.cancel()
                await asyncio.gather(slot.task, return_exceptions=True)

    @pytest.mark.asyncio
    async def test_closed_persisted_owner_survives_lossy_fold_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A closed dashboard transcript still owns its resumable sidecars."""
        direct_purges: list[str] = []
        matching_calls: list[tuple[set[str], set[str]]] = []

        def capture_matching(exact: set[str], folded: set[str], _fold) -> int:
            matching_calls.append((set(exact), set(folded)))
            return 0

        monkeypatch.setattr(session_ledger, "purge", direct_purges.append)
        monkeypatch.setattr(session_ledger, "purge_matching", capture_matching)
        state = _make_state({})
        state.conversation_log.list_sessions.return_value = [
            {"key": "dashboard_slack_team_direct_42"}
        ]

        await _remove_slot_for_history_key(state, "slack:team:direct:42")

        pin_keys = state.remove_chat_pins_for_slots.await_args.args[0]
        assert "slack_team_direct_42" not in pin_keys
        assert "slack_team_direct_42" not in direct_purges
        assert len(matching_calls) == 1
        ledger_exact, ledger_folded = matching_calls[0]
        assert "slack_team_direct_42" not in ledger_exact
        assert "slack_team_direct_42" not in ledger_folded

        override_exact, override_folded, _fold = (
            state.sessions.drop_autocompact_overrides_matching.call_args.args
        )
        assert "slack_team_direct_42" not in override_exact
        assert "dashboard:slack_team_direct_42" not in override_exact
        assert "slack_team_direct_42" not in override_folded

    @pytest.mark.asyncio
    async def test_legacy_slack_owner_survives_dashboard_fold_cleanup(
        self, monkeypatch: pytest.MonkeyPatch
    ):
        """A bare legacy Slack transcript still owns canonical Slack sidecars."""
        direct_purges: list[str] = []
        matching_calls: list[tuple[set[str], set[str]]] = []
        thread_ts = "1785370133.085469"
        slack_stem = f"slack_{thread_ts}"

        def capture_matching(exact: set[str], folded: set[str], _fold) -> int:
            matching_calls.append((set(exact), set(folded)))
            return 0

        monkeypatch.setattr(session_ledger, "purge", direct_purges.append)
        monkeypatch.setattr(session_ledger, "purge_matching", capture_matching)
        state = _make_state({})
        state.conversation_log.list_sessions.return_value = [{"key": thread_ts}]

        await _remove_slot_for_history_key(state, f"dashboard_{slack_stem}")

        pin_keys = state.remove_chat_pins_for_slots.await_args.args[0]
        assert slack_stem not in pin_keys
        assert slack_stem not in direct_purges
        assert len(matching_calls) == 1
        ledger_exact, ledger_folded = matching_calls[0]
        assert slack_stem not in ledger_exact
        assert slack_stem not in ledger_folded

        override_exact, override_folded, _fold = (
            state.sessions.drop_autocompact_overrides_matching.call_args.args
        )
        assert slack_stem not in override_exact
        assert f"slack:{thread_ts}" not in override_exact
        assert slack_stem not in override_folded

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "deleted_key",
        ("slack:team:direct:42", "slack_team_direct_42"),
        ids=("logical-key", "already-folded-stem"),
    )
    async def test_owner_scan_failure_skips_all_unproven_sidecar_cleanup(
        self,
        deleted_key: str,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
    ):
        """An unreadable owner catalog makes every request alias ambiguous."""
        direct_purges: list[str] = []
        matching_calls: list[tuple[set[str], set[str]]] = []

        def capture_matching(exact: set[str], folded: set[str], _fold) -> int:
            matching_calls.append((set(exact), set(folded)))
            return 0

        monkeypatch.setattr(session_ledger, "purge", direct_purges.append)
        monkeypatch.setattr(session_ledger, "purge_matching", capture_matching)
        state = _make_state({})
        state.conversation_log.list_sessions.side_effect = OSError("unreadable")

        with caplog.at_level(logging.DEBUG, logger="kiro_crew.dashboard.handlers.sessions"):
            await _remove_slot_for_history_key(state, deleted_key)

        pin_keys = state.remove_chat_pins_for_slots.await_args.args[0]
        assert pin_keys == set()
        assert direct_purges == []
        assert matching_calls == [(set(), set())]

        override_exact, override_folded, _fold = (
            state.sessions.drop_autocompact_overrides_matching.call_args.args
        )
        assert override_exact == set()
        assert override_folded == set()
        assert "persisted owner scan failed" in caplog.text

    @pytest.mark.asyncio
    async def test_running_task_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task.cancelled()
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_pending_question_cancelled_before_running_task(self):
        """History deletion must not leave a DashboardState-owned question
        future alive after its slot task and provider have been destroyed."""
        slot = _make_slot("dashboard_chat-1-100", running=True)
        state = _make_state({"dashboard_chat-1-100": slot})
        task_was_done: list[bool] = []

        def cancel_questions(slot_key: str) -> int:
            assert slot_key == slot.key
            task_was_done.append(slot.task.done())
            return 1

        state.cancel_questions_for_slot = MagicMock(side_effect=cancel_questions)

        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")

        state.cancel_questions_for_slot.assert_called_once_with(slot.key)
        assert task_was_done == [False]
        assert slot.task.cancelled()

    @pytest.mark.asyncio
    async def test_non_running_task_not_cancelled(self):
        slot = _make_slot("dashboard_chat-1-100", running=False)
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert slot.task is None
        state.sessions.destroy.assert_awaited_once_with("dashboard:chat-1-100")

    @pytest.mark.asyncio
    async def test_stacked_dashboard_prefix(self):
        slot = _make_slot("chat-3-300")
        state = _make_state({"chat-3-300": slot})
        await _remove_slot_for_history_key(state, "dashboard_dashboard_chat-3-300")
        assert "chat-3-300" not in state._slots

    @pytest.mark.asyncio
    async def test_batch_clear_removes_multiple_slots(self):
        """Verify batch clear removes matched slots and leaves unmatched."""
        slot_a = _make_slot("chat-1-100")
        slot_b = _make_slot("chat-2-200", running=True)
        slot_c = _make_slot("chat-9-999")
        state = _make_state(
            {
                "chat-1-100": slot_a,
                "chat-2-200": slot_b,
                "chat-9-999": slot_c,
            }
        )
        # Simulate batch clear for two keys (one matched, one running)
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        await _remove_slot_for_history_key(state, "dashboard_chat-2-200")
        assert "chat-1-100" not in state._slots
        assert "chat-2-200" not in state._slots
        assert "chat-9-999" in state._slots  # unmatched stays
        assert state.sessions.destroy.await_count == 2

    @pytest.mark.asyncio
    async def test_reverse_prefix_lookup(self):
        """A reverse-prefix alias is valid when the slot owns the bare history."""
        slot = _make_slot("dashboard_chat-1-100")
        slot.linked_session_key = "chat-1-100"
        state = _make_state({"dashboard_chat-1-100": slot})
        await _remove_slot_for_history_key(state, "chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots
        state.sessions.destroy.assert_awaited_once_with("chat-1-100")

    @pytest.mark.asyncio
    async def test_sessions_remove_exception_does_not_propagate(self):
        slot = _make_slot("dashboard_chat-1-100")
        state = _make_state({"dashboard_chat-1-100": slot})
        state.sessions.destroy = AsyncMock(side_effect=RuntimeError("already gone"))
        await _remove_slot_for_history_key(state, "dashboard_chat-1-100")
        assert "dashboard_chat-1-100" not in state._slots


class TestChannelSlotTeardown:
    """Deleting a channel history must tear down the CHANNEL's session.

    A channel-born slot runs the channel's own session, so a key derived from
    the history key names a session that does not exist: the provider survives
    the delete and its next inbound message recreates the transcript the user
    just removed.
    """

    @pytest.mark.asyncio
    async def test_destroys_the_slots_own_session_not_a_derived_key(self):
        slot = _make_slot("slack_1785370133.085469")
        slot.linked_session_key = "slack:1785370133.085469"
        state = _make_state({"slack_1785370133.085469": slot})

        await _remove_slot_for_history_key(state, "slack_1785370133.085469")

        state.sessions.destroy.assert_awaited_once_with("slack:1785370133.085469")
        assert "slack_1785370133.085469" not in state._slots
