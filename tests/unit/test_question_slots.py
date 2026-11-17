"""The optional limit on questions answered at the same time."""

import asyncio

from src.services import conversation_access


def test_no_limit_by_default(monkeypatch):
    monkeypatch.setattr(conversation_access.settings, "MAX_CONCURRENT_QUESTIONS", 0)
    assert conversation_access.question_slots() is None


def test_limit_lets_only_that_many_run_at_once(monkeypatch):
    monkeypatch.setattr(conversation_access.settings, "MAX_CONCURRENT_QUESTIONS", 2)
    monkeypatch.setattr(conversation_access, "_question_slots", None)

    async def scenario():
        slots = conversation_access.question_slots()
        running = peak = 0

        async def question():
            nonlocal running, peak
            async with slots:
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0.02)
                running -= 1

        await asyncio.gather(*(question() for _ in range(6)))
        return peak

    assert asyncio.run(scenario()) == 2


def test_a_waiting_question_that_is_cancelled_frees_nothing(monkeypatch):
    monkeypatch.setattr(conversation_access.settings, "MAX_CONCURRENT_QUESTIONS", 1)
    monkeypatch.setattr(conversation_access, "_question_slots", None)

    async def scenario():
        slots = conversation_access.question_slots()
        await slots.acquire()
        waiter = asyncio.create_task(slots.acquire())
        await asyncio.sleep(0.01)
        waiter.cancel()
        await asyncio.gather(waiter, return_exceptions=True)
        slots.release()
        # The only slot is free again: one acquire succeeds at once.
        await asyncio.wait_for(slots.acquire(), 0.5)
        return slots.locked()

    assert asyncio.run(scenario()) is True
