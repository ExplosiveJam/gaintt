import asyncio
import os
import subprocess
import sys
from pathlib import Path

import pytest

from kanbain.domain import Task
from kanbain.notifications import PlanNotifier
from kanbain.service import PlanService

CROSS_PROCESS_WRITER = Path(__file__).with_name("_cross_process_writer.py")
PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_successful_turn_publishes_plan_id_and_version_to_a_second_handle(tmp_path):
    """Same OS process, independent honker.Database handle on the same file."""
    database = tmp_path / "shared.sqlite"
    writer = PlanService(database)
    writer.initialize()
    plan = writer.create_plan("member", tasks={"a": Task(id="a", name="A", duration=1)})

    reader_notifier = PlanNotifier(database)

    async def observe():
        listener = reader_notifier.listen(fallback_poll_s=0.2)
        return await asyncio.wait_for(listener.__anext__(), timeout=5)

    async def main():
        listen_task = asyncio.create_task(observe())
        await asyncio.sleep(0.1)
        writer.apply_turn(plan.id, plan.version, [{"type": "reassign", "task_id": "a", "assignee": "Q"}])
        return await listen_task

    note = asyncio.run(main())

    assert note.channel == "plan"
    assert note.payload == {"plan_id": plan.id, "version": 2}


async def test_successful_turn_publishes_to_a_genuinely_separate_os_process(tmp_path):
    """A second, independently launched Python process writes; this process observes the signal."""
    database = tmp_path / "cross-process.sqlite"
    service = PlanService(database)
    service.initialize()
    plan = service.create_plan("member", tasks={"a": Task(id="a", name="A", duration=1)})

    reader_notifier = PlanNotifier(database)
    listener = reader_notifier.listen(fallback_poll_s=0.2)
    listen_task = asyncio.create_task(listener.__anext__())
    try:
        await asyncio.sleep(0.1)

        env = {**os.environ, "PYTHONPATH": str(PROJECT_ROOT)}
        completed = await asyncio.to_thread(
            subprocess.run,
            [sys.executable, str(CROSS_PROCESS_WRITER), str(database), plan.id, str(plan.version)],
            capture_output=True,
            text=True,
            timeout=20,
            env=env,
        )
        assert completed.returncode == 0, completed.stderr

        note = await asyncio.wait_for(listen_task, timeout=10)
    finally:
        if not listen_task.done():
            listen_task.cancel()
            try:
                await listen_task
            except (asyncio.CancelledError, StopAsyncIteration):
                pass

    assert note.channel == "plan"
    assert note.payload == {"plan_id": plan.id, "version": plan.version + 1}
    assert service.get_plan(plan.id).tasks["a"].assignee == "SUBPROCESS"


def test_listen_subscribes_synchronously_not_on_first_iteration(tmp_path):
    """Regression guard for P1-2: `listen()` must construct honker's Listener (and
    snapshot MAX(id)/subscribe to update events) synchronously, before returning --
    not defer that to the first `__anext__` call.

    kanbain/main.py's plan_events() relies on this: it calls
    `service.notifier.listen(...)` before reading the Plan's current version for
    its first SSE frame, specifically so a write landing in between is still
    captured. If `listen()` were a lazy `async def ... yield` wrapper (as it used
    to be), the real subscribe would happen only inside the `async for` loop --
    i.e. after the version read and after the first frame was already sent -- and
    a write in that window would be lost: its notification id would already be
    <= the (late) MAX(id) snapshot, so the listener would treat it as history
    rather than deliver it.

    This test never calls `service.get_plan` or touches the HTTP layer at all --
    it isolates the ordering guarantee at the source: the write happens strictly
    after `listen()` has already returned, which is only safe to miss if
    subscription is eager.
    """
    database = tmp_path / "eager-subscribe.sqlite"
    service = PlanService(database)
    service.initialize()
    plan = service.create_plan("member", tasks={"a": Task(id="a", name="A", duration=1)})

    notifier = PlanNotifier(database)
    listener = notifier.listen(fallback_poll_s=0.2)

    # This write happens strictly after listen() returned. A correct, eager
    # listener already snapshotted MAX(id) before this line runs, so the write's
    # notification must still be delivered.
    service.apply_turn(plan.id, plan.version, [{"type": "reassign", "task_id": "a", "assignee": "Q"}])

    note = asyncio.run(asyncio.wait_for(listener.__anext__(), timeout=5))

    assert note.payload == {"plan_id": plan.id, "version": 2}


def test_apply_turn_succeeds_even_when_publish_raises(tmp_path, monkeypatch):
    database = tmp_path / "publish-fails.sqlite"
    service = PlanService(database)
    service.initialize()
    plan = service.create_plan("member", tasks={"a": Task(id="a", name="A", duration=1)})

    def boom(*_args, **_kwargs):
        raise RuntimeError("honker is unreachable")

    monkeypatch.setattr(service.notifier, "publish", boom)

    result = service.apply_turn(plan.id, plan.version, [{"type": "reassign", "task_id": "a", "assignee": "Q"}])

    assert result.plan.version == 2
    assert service.get_plan(plan.id).tasks["a"].assignee == "Q"


def test_failed_write_does_not_publish(tmp_path):
    database = tmp_path / "no-publish-on-failure.sqlite"
    service = PlanService(database)
    service.initialize()
    plan = service.create_plan("member", tasks={"a": Task(id="a", name="A", duration=1)})

    published = []
    monkeypatch_publish = service.notifier.publish
    service.notifier.publish = lambda plan_id, version: published.append((plan_id, version))

    from kanbain.service import StalePlanError

    with pytest.raises(StalePlanError):
        service.apply_turn(plan.id, plan.version - 1 if plan.version > 0 else 999, [])

    assert published == []
    service.notifier.publish = monkeypatch_publish


def test_publish_itself_never_raises_out_of_the_notifier(tmp_path):
    notifier = PlanNotifier(tmp_path / "isolated.sqlite")
    # No bootstrap() was called, so the honker schema does not exist yet; publish must swallow the error.
    notifier.publish("plan-x", 1)


def test_prune_is_safe_to_call_and_returns_an_int(tmp_path):
    database = tmp_path / "prune.sqlite"
    service = PlanService(database)
    service.initialize()

    removed = service.notifier.prune(older_than_s=0, max_keep=0)

    assert isinstance(removed, int)
