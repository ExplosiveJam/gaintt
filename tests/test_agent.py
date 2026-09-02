import json
from datetime import date, timedelta

import pytest

from gaintt.agent import AgentService
from gaintt.domain import Task
from gaintt.service import PlanService, StalePlanError


@pytest.mark.asyncio
async def test_deterministic_chat_turn_moves_task_and_returns_neutral_stages(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="Релиз", duration=1)})

    result = await AgentService(service).handle(plan.id, "Перенеси задачу Релиз на неделю")

    assert result["plan"]["tasks"][0]["pinned_start"] == "2026-09-08"
    assert result["stages"] == ["анализирую", "проверяю", "применяю"]
    assert result["changes"]
    assert result["plan"]["turns"][0]["id"] == result["turn_id"]
    assert result["plan"]["turns"][0]["can_revert"] is True


@pytest.mark.asyncio
async def test_agent_self_repairs_invalid_tool_result_up_to_three_attempts(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    responses = iter(
        [
            {"mutations": [{"type": "reassign", "task_id": "missing", "assignee": "X"}]},
            {"mutations": [{"type": "reassign", "task_id": "a", "assignee": "X"}]},
        ]
    )

    result = await AgentService(service, model=lambda *_: next(responses)).handle(plan.id, "Сделай правку")

    assert result["plan"]["tasks"][0]["assignee"] == "X"
    assert result["attempts"] == 2


@pytest.mark.asyncio
async def test_openrouter_path_self_repairs_instead_of_stopping_after_first_error(tmp_path, monkeypatch):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    responses = iter(
        [
            {"mutations": [{"type": "reassign", "task_id": "missing", "assignee": "X"}]},
            {"mutations": [{"type": "reassign", "task_id": "a", "assignee": "X"}]},
        ]
    )
    agent = AgentService(service)

    async def fake_openrouter(*_):
        return next(responses)

    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setattr(agent, "_openrouter", fake_openrouter)

    result = await agent.handle(plan.id, "Сделай правку")

    assert result["attempts"] == 2
    assert result["plan"]["tasks"][0]["assignee"] == "X"


@pytest.mark.asyncio
async def test_ambiguous_task_name_returns_candidates_without_mutating(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan(
        "owner",
        tasks={
            "a": Task(id="a", name="Тестирование", assignee="Иванов", duration=1),
            "b": Task(id="b", name="Тестирование", assignee="Петров", duration=1),
        },
    )

    result = await AgentService(service).handle(plan.id, "Перенеси задачу Тестирование на неделю")

    assert result["clarification"] is True
    assert len(result["candidates"]) == 2
    assert service.get_plan(plan.id).version == 1


@pytest.mark.asyncio
async def test_bulk_after_phrase_is_one_atomic_turn(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan(
        "owner",
        tasks={
            "release": Task(id="release", name="Релиз", duration=1),
            "qa": Task(id="qa", name="Тестирование", duration=1, predecessors=["release"]),
            "demo": Task(id="demo", name="Демо", duration=1, predecessors=["qa"]),
        },
    )

    result = await AgentService(service).handle(plan.id, "Всё после Релиз — на неделю позже")

    assert result["attempts"] == 1
    assert result["plan"]["version"] == 2
    assert len(result["changes"]) == 2
    tasks = {task["id"]: task for task in result["plan"]["tasks"]}
    assert tasks["qa"]["start"] == "2026-09-09"
    assert tasks["demo"]["start"] == "2026-09-10"


@pytest.mark.asyncio
async def test_agent_can_add_task_after_named_predecessor(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"release": Task(id="release", name="Релиз", duration=1)})

    result = await AgentService(service).handle(plan.id, "Добавь задачу Согласовать демо после Релиз")

    added = next(task for task in result["plan"]["tasks"] if task["name"] == "Согласовать демо")
    assert added["predecessors"] == ["release"]


@pytest.mark.asyncio
async def test_stale_retry_reloads_plan_and_recomputes_relative_intent(tmp_path):
    service = PlanService(tmp_path / "stale-retry.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    calls = 0

    def relative_model(prompt, _history):
        nonlocal calls
        calls += 1
        snapshot = json.loads(prompt.split("\n", 1)[1])
        if calls == 1:
            service.apply_turn(plan.id, plan.version, [{"type": "pin_start", "task_id": "a", "date": "2026-09-04"}])
        start = date.fromisoformat(snapshot["tasks"][0]["start"])
        return {
            "mutations": [{"type": "pin_start", "task_id": "a", "date": (start + timedelta(days=7)).isoformat()}],
            "reply": "Сдвинуто на неделю.",
        }

    result = await AgentService(service, model=relative_model).handle(plan.id, "Перенеси A на неделю")

    assert calls == 2
    assert result["attempts"] == 2
    assert result["plan"]["tasks"][0]["pinned_start"] == "2026-09-11"


@pytest.mark.asyncio
async def test_deterministic_stale_retry_does_not_depend_on_error_text(tmp_path):
    class MessageIndependentService(PlanService):
        def apply_turn(self, plan_id, base_version, mutations):
            try:
                return super().apply_turn(plan_id, base_version, mutations)
            except StalePlanError as exc:
                raise StalePlanError("conflict wording may change") from exc

    service = MessageIndependentService(tmp_path / "deterministic-stale.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    agent = AgentService(service)
    original_call_tool = agent.client.call_tool
    raced = False

    async def racing_call_tool(name, arguments):
        nonlocal raced
        if name == "apply_turn" and not raced:
            raced = True
            service.apply_turn(plan.id, plan.version, [{"type": "pin_start", "task_id": "a", "date": "2026-09-04"}])
        return await original_call_tool(name, arguments)

    agent.client.call_tool = racing_call_tool
    result = await agent.handle(plan.id, "Перенеси задачу A на неделю")

    assert result["attempts"] == 2
    assert result["plan"]["tasks"][0]["pinned_start"] == "2026-09-11"


@pytest.mark.asyncio
async def test_agent_reports_one_attempt_when_deterministic_mutation_fails_immediately(tmp_path):
    service = PlanService(tmp_path / "one-attempt.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})

    result = await AgentService(service).handle(plan.id, "Сделай A после A")

    assert result["attempts"] == 1
    assert result["error"]


@pytest.mark.asyncio
async def test_agent_does_not_create_a_turn_for_an_explicit_clarification_reply(tmp_path):
    service = PlanService(tmp_path / "clarification.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A")})

    def model(*_):
        return {"mutations": [], "reply": "Уточните, какую задачу изменить."}

    result = await AgentService(service, model=model).handle(plan.id, "Измени план")

    assert result["reply"] == "Уточните, какую задачу изменить."
    assert result["changes"] == []
    assert result["attempts"] == 1
    assert "turn_id" not in result
    assert service.get_plan(plan.id).version == plan.version
    assert service.list_turns(plan.id) == []


@pytest.mark.asyncio
async def test_agent_retries_a_model_response_that_omits_mutations_instead_of_reporting_success(tmp_path):
    service = PlanService(tmp_path / "invalid-model-response.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A")})
    calls = 0

    def invalid_model(*_):
        nonlocal calls
        calls += 1
        return {"reply": "Готово."}

    result = await AgentService(service, model=invalid_model).handle(plan.id, "Измени план")

    assert calls == 3
    assert result["attempts"] == 3
    assert result["error"]
    assert service.get_plan(plan.id).version == plan.version
    assert service.list_turns(plan.id) == []
