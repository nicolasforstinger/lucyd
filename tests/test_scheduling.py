"""Tests for scheduling tools — schedule_message and list_scheduled."""

import asyncio
from unittest.mock import AsyncMock

import pytest

import tools.scheduling as sched
from tools.scheduling import configure, tool_list_scheduled, tool_schedule_message


@pytest.fixture(autouse=True)
def reset_module_state():
    """Reset module-level state between tests."""
    original_channel = sched._channel
    original_counter = sched._counter
    yield
    # Cancel any lingering tasks
    for info in sched._scheduled.values():
        if not info["task"].done():
            info["task"].cancel()
    sched._channel = original_channel
    sched._scheduled.clear()
    sched._counter = original_counter


# ─── tool_schedule_message ───────────────────────────────────────


class TestScheduleMessage:
    @pytest.mark.asyncio
    async def test_no_channel(self):
        configure(channel=None)
        result = await tool_schedule_message("Nicolas", "hi", 60)
        assert "No channel configured" in result

    @pytest.mark.asyncio
    async def test_negative_delay(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "hi", -1)
        assert "must be positive" in result

    @pytest.mark.asyncio
    async def test_zero_delay(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "hi", 0)
        assert "must be positive" in result

    @pytest.mark.asyncio
    async def test_exceeds_max_delay(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "hi", 86401)
        assert "Maximum delay" in result

    @pytest.mark.asyncio
    async def test_empty_text(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "", 60)
        assert "Message text is required" in result

    @pytest.mark.asyncio
    async def test_successful_schedule_seconds(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "hello", 30)
        assert "sched-" in result
        assert "30s" in result

    @pytest.mark.asyncio
    async def test_successful_schedule_minutes(self):
        configure(channel=AsyncMock())
        result = await tool_schedule_message("Nicolas", "hello", 300)
        assert "5m" in result

    @pytest.mark.asyncio
    async def test_message_fires_and_sends(self):
        """Scheduled message actually sends via channel after delay."""
        ch = AsyncMock()
        configure(channel=ch)
        await tool_schedule_message("Nicolas", "delayed hello", 1)

        # Wait for the timer to fire
        await asyncio.sleep(1.5)

        ch.send.assert_awaited_once_with("Nicolas", "delayed hello", None)

    @pytest.mark.asyncio
    async def test_task_cleaned_up_after_fire(self):
        ch = AsyncMock()
        configure(channel=ch)
        result = await tool_schedule_message("Nicolas", "bye", 1)
        sched_id = result.split("(")[1].rstrip(")")

        await asyncio.sleep(1.5)
        assert sched_id not in sched._scheduled

    @pytest.mark.asyncio
    async def test_counter_increments(self):
        configure(channel=AsyncMock())
        r1 = await tool_schedule_message("Nicolas", "a", 60)
        r2 = await tool_schedule_message("Nicolas", "b", 60)
        # Extract IDs
        id1 = r1.split("(")[1].rstrip(")")
        id2 = r2.split("(")[1].rstrip(")")
        assert id1 != id2


# ─── tool_list_scheduled ────────────────────────────────────────


class TestListScheduled:
    @pytest.mark.asyncio
    async def test_empty_list(self):
        configure(channel=AsyncMock())
        result = await tool_list_scheduled()
        assert "No scheduled messages" in result

    @pytest.mark.asyncio
    async def test_shows_pending_messages(self):
        ch = AsyncMock()
        configure(channel=ch)
        await tool_schedule_message("Nicolas", "reminder", 300)
        await tool_schedule_message("Nicolas", "follow-up", 600)

        result = await tool_list_scheduled()
        assert "sched-" in result
        assert "Nicolas" in result
        assert "reminder" in result
        assert "follow-up" in result

    @pytest.mark.asyncio
    async def test_schedule_cap_reached(self):
        """Filling to 50 scheduled messages, 51st returns error."""
        ch = AsyncMock()
        configure(channel=ch)
        for i in range(50):
            result = await tool_schedule_message("Nicolas", f"msg-{i}", 3600)
            assert "sched-" in result
        # 51st should be rejected
        result = await tool_schedule_message("Nicolas", "overflow", 3600)
        assert "Maximum 50 scheduled messages reached" in result

    @pytest.mark.asyncio
    async def test_schedule_cap_allows_after_cleanup(self):
        """Fill to 50, remove one, verify next succeeds."""
        ch = AsyncMock()
        configure(channel=ch)
        for i in range(50):
            await tool_schedule_message("Nicolas", f"msg-{i}", 3600)
        # Remove one
        first_id = list(sched._scheduled.keys())[0]
        task = sched._scheduled.pop(first_id)
        task["task"].cancel()
        # Now should be able to add one more
        result = await tool_schedule_message("Nicolas", "after-cleanup", 3600)
        assert "sched-" in result

    @pytest.mark.asyncio
    async def test_excludes_completed(self):
        ch = AsyncMock()
        configure(channel=ch)
        await tool_schedule_message("Nicolas", "fast", 1)

        await asyncio.sleep(1.5)

        result = await tool_list_scheduled()
        assert "No scheduled messages" in result


# ─── TEST-9: Fire-and-cleanup with minimal delay ─────────────────


class TestScheduleFireAndCleanup:
    """TEST-9: Schedule with minimal delay, verify send + cleanup."""

    @pytest.mark.asyncio
    async def test_minimal_delay_fires_and_cleans_up(self):
        """Schedule with 0.1s delay, verify send called and _scheduled empty."""
        ch = AsyncMock()
        configure(channel=ch)

        result = await tool_schedule_message("Nicolas", "quick reminder", 0.1)
        sched_id = result.split("(")[1].rstrip(")")

        # Immediately after scheduling, the task should be tracked
        assert sched_id in sched._scheduled

        # Wait for the timer to fire
        await asyncio.sleep(0.3)

        # Verify the message was sent
        ch.send.assert_awaited_once_with("Nicolas", "quick reminder", None)

        # Verify cleanup: _scheduled dict no longer has this entry
        assert sched_id not in sched._scheduled
        assert len(sched._scheduled) == 0

    @pytest.mark.asyncio
    async def test_two_minimal_timers_both_fire(self):
        """Two messages with small delays both fire and clean up."""
        ch = AsyncMock()
        configure(channel=ch)

        r1 = await tool_schedule_message("Alice", "msg-a", 0.1)
        r2 = await tool_schedule_message("Bob", "msg-b", 0.15)

        id1 = r1.split("(")[1].rstrip(")")
        id2 = r2.split("(")[1].rstrip(")")

        assert id1 in sched._scheduled
        assert id2 in sched._scheduled

        await asyncio.sleep(0.4)

        # Both should have fired
        assert ch.send.await_count == 2

        # Verify both recipients received their message
        calls = ch.send.await_args_list
        sent_pairs = {(c.args[0], c.args[1]) for c in calls}
        assert ("Alice", "msg-a") in sent_pairs
        assert ("Bob", "msg-b") in sent_pairs

        # Both cleaned up
        assert id1 not in sched._scheduled
        assert id2 not in sched._scheduled
        assert len(sched._scheduled) == 0

    @pytest.mark.asyncio
    async def test_send_failure_still_cleans_up(self):
        """If channel.send raises, the scheduled entry is still removed."""
        ch = AsyncMock()
        ch.send.side_effect = RuntimeError("network error")
        configure(channel=ch)

        result = await tool_schedule_message("Nicolas", "will fail", 0.1)
        sched_id = result.split("(")[1].rstrip(")")

        await asyncio.sleep(0.3)

        # send was attempted
        ch.send.assert_awaited_once()

        # Entry should still be cleaned up even after failure
        assert sched_id not in sched._scheduled
