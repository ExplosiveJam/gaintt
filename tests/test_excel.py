from datetime import date
from io import BytesIO

from openpyxl import Workbook

from gaintt.domain import Plan, Task
from gaintt.excel import export_plan, import_plan


def workbook_bytes(headers, rows):
    workbook = Workbook()
    sheet = workbook.active
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_import_accepts_ru_en_headers_and_three_predecessor_separators():
    data = workbook_bytes(
        [" TASK ", "Description", "ИСПОЛНИТЕЛЬ", "duration", "Предшественники"],
        [
            ["A", "first", "I", 1, ""],
            ["B", "second", "P", 2, "A; A\nA"],
        ],
    )

    plan, report = import_plan(data)

    assert report.loaded_count == 2
    assert plan.tasks["task-002"].predecessors == ["task-001"]
    assert not report.errors


def test_import_report_names_unknown_duplicate_cycle_and_bad_duration():
    data = workbook_bytes(
        ["задача", "описание", "исполнитель", "длительность", "предшественники"],
        [
            ["A", "", "I", 1, "Missing"],
            ["A", "duplicate", "P", 2, "A"],
            ["B", "", "S", "n/a", ""],
        ],
    )

    plan, report = import_plan(data)

    assert report.loaded_count == 2
    assert any("не существует" in item for item in report.warnings)
    assert any("неоднознач" in item for item in report.warnings)
    assert any("длитель" in item for item in report.errors)


def test_round_trip_preserves_plan_fields_and_schedule_sheet():
    plan = Plan(
        id="round-trip",
        name="Round trip",
        plan_start=date(2026, 9, 1),
        tasks={
            "a": Task(
                id="a",
                name="A",
                description="desc",
                assignee="I",
                duration=2,
                pinned_start=date(2026, 9, 3),
                due_date=date(2026, 9, 8),
            ),
            "b": Task(id="b", name="B", assignee="P", duration=1, predecessors=["a"]),
        },
    )

    exported = export_plan(plan)
    imported, report = import_plan(exported)

    assert not report.errors
    assert imported.plan_start == plan.plan_start
    assert imported.name == plan.name
    assert imported.tasks["a"].to_public_name_dict() == plan.tasks["a"].to_public_name_dict()
    assert imported.tasks["b"].predecessors == ["a"]


def test_import_breaks_cycle_at_closing_link_and_reports_it():
    data = workbook_bytes(
        ["задача", "описание", "исполнитель", "длительность", "предшественники"],
        [["A", "", "I", 1, "C"], ["B", "", "P", 1, "A"], ["C", "", "S", 1, "B"]],
    )

    plan, report = import_plan(data)

    assert any("Цикл разорван" in item for item in report.warnings)
    plan.schedule()
    assert sum(len(task.predecessors) for task in plan.tasks.values()) == 2


def test_import_replaces_duplicate_ids_without_overwriting_a_task():
    data = workbook_bytes(
        ["задача", "длительность", "ID"],
        [["A", 1, "task-002"], ["B", 1, "task-002"], ["C", 1, "task-003"]],
    )

    plan, report = import_plan(data)

    assert report.loaded_count == 3
    assert len(plan.tasks) == 3
    assert {task.name for task in plan.tasks.values()} == {"A", "B", "C"}
    assert any("Дублирующийся ID" in item for item in report.warnings)


def test_import_uses_first_plan_start_and_reports_an_invalid_value_leniently():
    data = workbook_bytes(
        ["задача", "длительность", "Plan Start"],
        [["A", 1, "not-a-date"], ["B", 1, "2026-10-20"]],
    )

    plan, report = import_plan(data, default_plan_start="2026-09-01")

    assert report.loaded_count == 2
    assert plan.plan_start == date(2026, 9, 1)
    assert any("начала плана" in item for item in report.errors)


def test_import_does_not_mix_plan_metadata_from_different_rows():
    data = workbook_bytes(
        ["задача", "длительность", "Plan Start", "Plan Name"],
        [["A", 1, "", "Первый план"], ["B", 1, "2026-10-20", "Другой план"]],
    )

    plan, _ = import_plan(data, default_plan_start="2026-09-01")

    assert plan.name == "Первый план"
    assert plan.plan_start == date(2026, 9, 1)
