"""Dependency-free domain model and forward-pass scheduler."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

DATE_FORMAT = "%Y-%m-%d"


class DomainValidationError(ValueError):
    """A mutation cannot produce a valid Plan."""

    def __init__(self, message: str, code: str = "validation_error") -> None:
        super().__init__(message)
        self.code = code


def parse_date(value: Any) -> Optional[date]:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError as exc:
        raise DomainValidationError(f"Invalid date: {value}", "invalid_date") from exc


def date_value(value: Optional[date]) -> Optional[str]:
    return value.isoformat() if value else None


@dataclass
class Task:
    id: str
    name: str
    description: str = ""
    assignee: str = ""
    duration: int = 1
    predecessors: List[str] = field(default_factory=list)
    pinned_start: Optional[date] = None
    due_date: Optional[date] = None

    def __post_init__(self) -> None:
        self.name = str(self.name).strip()
        self.description = str(self.description or "")
        self.assignee = str(self.assignee or "")
        self.duration = int(self.duration)
        if not self.name:
            raise DomainValidationError("Task name must not be empty", "invalid_task")
        if self.duration < 1:
            raise DomainValidationError("Task duration must be at least 1 day", "invalid_duration")
        self.predecessors = list(dict.fromkeys(str(item) for item in self.predecessors))
        self.pinned_start = parse_date(self.pinned_start)
        self.due_date = parse_date(self.due_date)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "assignee": self.assignee,
            "duration": self.duration,
            "predecessors": list(self.predecessors),
            "pinned_start": date_value(self.pinned_start),
            "due_date": date_value(self.due_date),
        }

    def to_public_name_dict(self) -> Dict[str, Any]:
        """Fields represented by the human-facing Plan sheet, excluding generated ids."""
        result = self.to_dict()
        result.pop("id", None)
        return result

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Task":
        return cls(
            id=str(value["id"]),
            name=value["name"],
            description=value.get("description", ""),
            assignee=value.get("assignee", ""),
            duration=value.get("duration", 1),
            predecessors=value.get("predecessors", []),
            pinned_start=value.get("pinned_start"),
            due_date=value.get("due_date"),
        )


@dataclass(frozen=True)
class ScheduleItem:
    start: date
    finish: date

    @property
    def last_day(self) -> date:
        return self.finish - timedelta(days=1)

    def to_dict(self) -> Dict[str, str]:
        return {"start": self.start.isoformat(), "finish": self.finish.isoformat(), "last_day": self.last_day.isoformat()}


@dataclass
class Plan:
    id: str
    name: str
    plan_start: date
    tasks: Dict[str, Task] = field(default_factory=dict)
    version: int = 1

    def __post_init__(self) -> None:
        self.plan_start = parse_date(self.plan_start) or date.today()
        self.tasks = dict(self.tasks)
        self.version = int(self.version)

    def clone(self) -> "Plan":
        return Plan.from_dict(self.to_dict())

    def schedule(self) -> Dict[str, ScheduleItem]:
        """Compute all task dates with a topological forward pass.

        ``finish`` is exclusive. A predecessor's finish is therefore exactly the
        successor's earliest possible start.
        """
        states: Dict[str, int] = {task_id: 0 for task_id in self.tasks}
        result: Dict[str, ScheduleItem] = {}

        def visit(task_id: str) -> ScheduleItem:
            if task_id not in self.tasks:
                raise DomainValidationError(f"Task '{task_id}' does not exist", "unknown_task")
            if states[task_id] == 1:
                raise DomainValidationError(f"Dependency cycle detected at Task '{task_id}'", "cycle")
            if states[task_id] == 2:
                return result[task_id]
            states[task_id] = 1
            task = self.tasks[task_id]
            earliest = self.plan_start
            for predecessor_id in task.predecessors:
                predecessor = visit(predecessor_id)
                earliest = max(earliest, predecessor.finish)
            if task.pinned_start:
                earliest = max(earliest, task.pinned_start)
            item = ScheduleItem(earliest, earliest + timedelta(days=task.duration))
            result[task_id] = item
            states[task_id] = 2
            return item

        for task_id in self.tasks:
            visit(task_id)
        return result

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "plan_start": self.plan_start.isoformat(),
            "version": self.version,
            "tasks": [task.to_dict() for task in self.tasks.values()],
        }

    def to_public_dict(self) -> Dict[str, Any]:
        schedule = self.schedule()
        tasks = []
        for task in self.tasks.values():
            item = task.to_dict()
            item["start"] = schedule[task.id].start.isoformat()
            item["finish"] = schedule[task.id].finish.isoformat()
            item["last_day"] = schedule[task.id].last_day.isoformat()
            item["overdue"] = bool(task.due_date and schedule[task.id].last_day > task.due_date)
            tasks.append(item)
        links = [
            {"id": f"{predecessor_id}->{task.id}", "source": predecessor_id, "target": task.id, "type": "e2s"}
            for task in self.tasks.values()
            for predecessor_id in task.predecessors
        ]
        return {
            "id": self.id,
            "name": self.name,
            "plan_start": self.plan_start.isoformat(),
            "version": self.version,
            "tasks": tasks,
            "links": links,
        }

    @classmethod
    def from_dict(cls, value: Dict[str, Any]) -> "Plan":
        tasks = {item["id"]: Task.from_dict(item) for item in value.get("tasks", [])}
        return cls(
            id=str(value["id"]),
            name=value.get("name", "Без названия"),
            plan_start=value.get("plan_start"),
            tasks=tasks,
            version=value.get("version", 1),
        )


def _date_for_shift(plan: Plan, mutation: Dict[str, Any]) -> date:
    requested_start = mutation.get("start", mutation.get("date"))
    if requested_start is not None:
        parsed = parse_date(requested_start)
        if parsed is not None:
            return parsed
    current = plan.schedule()[mutation["task_id"]].start
    return current + timedelta(days=int(mutation.get("days", 0)))


def _apply_one(plan: Plan, mutation: Dict[str, Any]) -> None:
    kind = mutation.get("type")
    task_id = mutation.get("task_id")
    if kind == "add_task":
        new_id = str(mutation.get("id") or f"task-{len(plan.tasks) + 1:03d}")
        if new_id in plan.tasks:
            raise DomainValidationError(f"Task '{new_id}' already exists", "duplicate_task")
        plan.tasks[new_id] = Task(
            id=new_id,
            name=mutation.get("name", "Новая задача"),
            description=mutation.get("description", ""),
            assignee=mutation.get("assignee", ""),
            duration=mutation.get("duration", 1),
            predecessors=mutation.get("predecessor_ids", mutation.get("predecessors", [])),
            pinned_start=mutation.get("pinned_start"),
            due_date=mutation.get("due_date"),
        )
        return
    if task_id not in plan.tasks:
        raise DomainValidationError(f"Task '{task_id}' does not exist", "unknown_task")
    task = plan.tasks[task_id]
    if kind in ("pin_start", "shift_task"):
        task.pinned_start = _date_for_shift(plan, mutation)
    elif kind == "clear_pinned_start":
        task.pinned_start = None
    elif kind == "reassign":
        task.assignee = str(mutation.get("assignee", ""))
    elif kind == "set_due_date":
        task.due_date = parse_date(mutation.get("due_date"))
    elif kind == "set_predecessors":
        predecessors = mutation.get("predecessor_ids", mutation.get("predecessors", []))
        if not isinstance(predecessors, list):
            raise DomainValidationError("predecessor_ids must be a list", "invalid_predecessors")
        task.predecessors = list(dict.fromkeys(str(item) for item in predecessors))
    elif kind == "remove_task":
        del plan.tasks[task_id]
        for other in plan.tasks.values():
            other.predecessors = [item for item in other.predecessors if item != task_id]
    elif kind == "rename":
        task.name = str(mutation.get("name", "")).strip()
        if not task.name:
            raise DomainValidationError("Task name must not be empty", "invalid_task")
    elif kind == "update_task":
        for field_name in ("name", "description", "assignee", "duration", "pinned_start", "due_date"):
            if field_name in mutation:
                setattr(task, field_name, mutation[field_name])
        task.__post_init__()
    else:
        raise DomainValidationError(f"Unknown mutation type: {kind}", "unknown_mutation")


def _replace_plan(target: Plan, source: Plan) -> None:
    target.name = source.name
    target.plan_start = source.plan_start
    target.tasks = source.tasks


def apply_mutations(plan: Plan, mutations: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Validate and apply all mutations atomically to ``plan``."""
    before = plan.clone()
    candidate = plan.clone()
    mutation_list = list(mutations)
    for mutation in mutation_list:
        _apply_one(candidate, dict(mutation))
        # Catch dangling predecessor ids and cycles before the next mutation can hide them.
        candidate.schedule()
    candidate.schedule()
    _replace_plan(plan, candidate)

    before_schedule = before.schedule()
    after_schedule = plan.schedule()
    changes: List[Dict[str, Any]] = []
    for task_id, task in plan.tasks.items():
        previous = before.tasks.get(task_id)
        if previous is None:
            changes.append({"task_id": task_id, "kind": "added", "label": f"Добавлена: {task.name}"})
            continue
        if previous.to_dict() != task.to_dict() or before_schedule.get(task_id) != after_schedule.get(task_id):
            changes.append({"task_id": task_id, "kind": "updated", "label": f"Изменена: {task.name}"})
    for task_id, task in before.tasks.items():
        if task_id not in plan.tasks:
            changes.append({"task_id": task_id, "kind": "removed", "label": f"Удалена: {task.name}"})
    return changes


def seed_plan(plan_id: str) -> Plan:
    """A compact but meaningful demo plan with several chains and one overdue task."""
    names = [
        ("discovery", "Сформулировать гипотезу", "Иванов", 2, []),
        ("scope", "Зафиксировать объём", "Иванов", 2, ["discovery"]),
        ("design", "Спроектировать интерфейс", "Петров", 4, ["scope"]),
        ("copy", "Подготовить тексты", "Сидорова", 2, ["scope"]),
        ("api", "Спроектировать API", "Петров", 3, ["scope"]),
        ("db", "Подготовить схему данных", "Иванов", 2, ["api"]),
        ("frontend", "Собрать экран плана", "Сидорова", 5, ["design", "copy"]),
        ("backend", "Реализовать API", "Петров", 5, ["api", "db"]),
        ("gantt", "Подключить диаграмму Гантта", "Сидорова", 3, ["frontend", "backend"]),
        ("import", "Добавить импорт Excel", "Иванов", 3, ["backend"]),
        ("export", "Добавить экспорт Excel", "Иванов", 2, ["import"]),
        ("agent", "Подключить агента", "Петров", 4, ["backend"]),
        ("chat", "Собрать чат", "Сидорова", 3, ["agent", "frontend"]),
        ("modal", "Сделать карточку задачи", "Сидорова", 2, ["gantt"]),
        ("undo", "Добавить откат хода", "Петров", 2, ["chat"]),
        ("smoke", "Проверить главный сценарий", "Иванов", 2, ["export", "undo", "modal"]),
        ("deploy", "Развернуть демо", "Петров", 1, ["smoke"]),
        ("demo", "Записать демо", "Сидорова", 1, ["deploy"]),
    ]
    tasks = {
        task_id: Task(
            id=task_id,
            name=name,
            assignee=assignee,
            duration=duration,
            predecessors=predecessors,
            due_date=date(2026, 9, 10) if task_id == "gantt" else None,
        )
        for task_id, name, assignee, duration, predecessors in names
    }
    return Plan(plan_id, "Демо-план Kanbain", date(2026, 9, 1), tasks)
