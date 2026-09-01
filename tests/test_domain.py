from datetime import date

import pytest

from kanbain.domain import DomainValidationError, Plan, Task, apply_mutations


def make_plan() -> Plan:
    return Plan(
        id="plan-1",
        name="Demo",
        plan_start=date(2026, 9, 1),
        tasks={
            "a": Task(id="a", name="Подготовка", assignee="Иванов", duration=1),
            "b": Task(id="b", name="Разработка", assignee="Петров", duration=2, predecessors=["a"]),
            "c": Task(id="c", name="Релиз", assignee="Сидорова", duration=1, predecessors=["b"]),
        },
    )


def test_schedule_uses_inclusive_last_day_and_next_day_successor():
    plan = make_plan()

    schedule = plan.schedule()

    assert schedule["a"].start == date(2026, 9, 1)
    assert schedule["a"].finish == date(2026, 9, 2)
    assert schedule["b"].start == date(2026, 9, 2)
    assert schedule["b"].finish == date(2026, 9, 4)
    assert schedule["c"].start == date(2026, 9, 4)
    assert schedule["c"].last_day == date(2026, 9, 4)


def test_pinned_start_is_a_not_before_constraint():
    plan = make_plan()
    plan.tasks["b"].pinned_start = date(2026, 8, 1)

    assert plan.schedule()["b"].start == date(2026, 9, 2)

    plan.tasks["b"].pinned_start = date(2026, 9, 10)
    assert plan.schedule()["b"].start == date(2026, 9, 10)


def test_apply_mutations_is_atomic_for_cycle_and_unknown_task():
    plan = make_plan()
    original = plan.to_dict()

    with pytest.raises(DomainValidationError, match="cycle"):
        apply_mutations(plan, [{"type": "set_predecessors", "task_id": "a", "predecessor_ids": ["c"]}])
    assert plan.to_dict() == original

    with pytest.raises(DomainValidationError, match="does not exist"):
        apply_mutations(
            plan,
            [
                {"type": "reassign", "task_id": "a", "assignee": "Новый"},
                {"type": "pin_start", "task_id": "missing", "date": "2026-09-10"},
            ],
        )
    assert plan.to_dict() == original


def test_apply_mutations_returns_changes_and_supports_bulk_style_list():
    plan = make_plan()

    changes = apply_mutations(
        plan,
        [
            {"type": "reassign", "task_id": "a", "assignee": "Петров"},
            {"type": "pin_start", "task_id": "a", "date": "2026-09-03"},
        ],
    )

    assert plan.tasks["a"].assignee == "Петров"
    assert plan.schedule()["a"].start == date(2026, 9, 3)
    assert {change["task_id"] for change in changes} == {"a", "b", "c"}
