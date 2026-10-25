import asyncio

import pytest

from bridge.main import run_until_first_done


@pytest.mark.asyncio
async def test_returns_and_cancels_others_when_first_finishes():
    cancelled = asyncio.Event()

    async def polling():
        await asyncio.sleep(0.05)

    async def forever():
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            cancelled.set()
            raise

    await asyncio.wait_for(run_until_first_done(polling(), forever()), timeout=2)
    assert cancelled.is_set()


@pytest.mark.asyncio
async def test_propagates_error_from_first_finished_task():
    async def boom():
        raise RuntimeError("упало")

    async def forever():
        await asyncio.sleep(3600)

    with pytest.raises(RuntimeError, match="упало"):
        await asyncio.wait_for(run_until_first_done(boom(), forever()), timeout=2)


@pytest.mark.asyncio
async def test_does_not_hang_on_task_ignoring_cancel():
    async def polling():
        await asyncio.sleep(0.01)

    release = asyncio.Event()

    async def stubborn():
        # игнорирует отмену, пока тест не отпустит — чтобы не зависнуть при закрытии цикла
        while not release.is_set():
            try:
                await asyncio.sleep(0.05)
            except asyncio.CancelledError:
                continue

    try:
        await asyncio.wait_for(
            run_until_first_done(polling(), stubborn(), cancel_timeout=0.2), timeout=2
        )
    finally:
        release.set()
