import sqlite3

import pytest

from gaintt.domain import DomainValidationError, Task
from gaintt.service import PlanNotFoundError, PlanService, StalePlanError


def test_apply_turn_is_versioned_atomic_and_revert_is_guarded(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("member-1", tasks={"a": Task(id="a", name="A", duration=1)})

    result = service.apply_turn(
        plan.id,
        plan.version,
        [{"type": "reassign", "task_id": "a", "assignee": "Иванов"}],
    )
    assert result.plan.version == 2
    assert result.plan.tasks["a"].assignee == "Иванов"

    with pytest.raises(StalePlanError):
        service.apply_turn(plan.id, plan.version, [{"type": "reassign", "task_id": "a", "assignee": "X"}])

    reverted = service.revert_turn(result.turn_id)
    assert reverted.plan.version == 3
    assert reverted.plan.tasks["a"].assignee == ""

    with pytest.raises(StalePlanError):
        service.revert_turn(result.turn_id)


@pytest.mark.parametrize(
    "mutations",
    [[], [{"type": "reassign", "task_id": "a", "assignee": ""}]],
    ids=["empty", "semantic-no-op"],
)
def test_apply_turn_rejects_empty_or_unchanged_turn_without_incrementing_version(tmp_path, mutations):
    service = PlanService(tmp_path / "no-op.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A")})

    with pytest.raises(DomainValidationError, match="does not change"):
        service.apply_turn(plan.id, plan.version, mutations)

    assert service.get_plan(plan.id).version == plan.version
    assert service.list_turns(plan.id) == []


def test_new_owner_gets_independent_seed_plan(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    first = service.get_or_create_plan("owner-a")
    second = service.get_or_create_plan("owner-b")

    assert first.id != second.id
    assert first.tasks.keys() == second.tasks.keys()


def test_plan_survives_service_restart_on_same_sqlite_file(tmp_path):
    database = tmp_path / "plans.sqlite"
    first_service = PlanService(database)
    first_service.initialize()
    plan = first_service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    first_service.apply_turn(
        plan.id,
        1,
        [{"type": "reassign", "task_id": "a", "assignee": "Иванов"}],
    )

    restarted_service = PlanService(database)
    restarted_service.initialize()
    restored = restarted_service.get_plan(plan.id)

    assert restored.version == 2
    assert restored.tasks["a"].assignee == "Иванов"


def test_service_closes_connections_after_success_and_error(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    opened = []

    class TrackingConnection(sqlite3.Connection):
        was_closed = False

        def close(self):
            self.was_closed = True
            super().close()

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=TrackingConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr("gaintt.service.sqlite3.connect", tracking_connect)
    service = PlanService(tmp_path / "connections.sqlite")

    service.initialize()
    with pytest.raises(PlanNotFoundError):
        service.get_plan("missing")

    assert len(opened) == 2
    assert all(connection.was_closed for connection in opened)


def test_plan_members_are_explicit_not_derived_from_a_single_owner(tmp_path):
    service = PlanService(tmp_path / "members.sqlite")
    service.initialize()
    plan = service.create_plan("creator")

    assert service.is_member(plan.id, "creator") is True
    assert service.is_member(plan.id, "stranger") is False

    service.add_member(plan.id, "stranger")

    assert service.is_member(plan.id, "stranger") is True
    # Membership is stored, not derived from the plan row itself.
    with sqlite3.connect(tmp_path / "members.sqlite") as connection:
        rows = connection.execute("SELECT member_id FROM plan_members WHERE plan_id = ? ORDER BY member_id", (plan.id,)).fetchall()
    assert [row[0] for row in rows] == ["creator", "stranger"]


def test_add_member_on_missing_plan_raises_not_found(tmp_path):
    service = PlanService(tmp_path / "missing-plan.sqlite")
    service.initialize()

    with pytest.raises(PlanNotFoundError):
        service.add_member("does-not-exist", "someone")

    with pytest.raises(PlanNotFoundError):
        service.is_member("does-not-exist", "someone")


def test_member_count_reflects_explicit_membership_rows(tmp_path):
    service = PlanService(tmp_path / "member-count.sqlite")
    service.initialize()
    plan = service.create_plan("creator")

    assert service.member_count(plan.id) == 1

    service.add_member(plan.id, "invitee")

    assert service.member_count(plan.id) == 2
    # Re-adding an existing member must not inflate the count.
    service.add_member(plan.id, "invitee")
    assert service.member_count(plan.id) == 2


def test_plan_id_for_turn_is_used_for_membership_checks_on_revert(tmp_path):
    service = PlanService(tmp_path / "turn-membership.sqlite")
    service.initialize()
    plan = service.create_plan("creator", tasks={"a": Task(id="a", name="A", duration=1)})
    result = service.apply_turn(plan.id, plan.version, [{"type": "reassign", "task_id": "a", "assignee": "Q"}])

    assert service.plan_id_for_turn(result.turn_id) == plan.id
    with pytest.raises(PlanNotFoundError):
        service.plan_id_for_turn("does-not-exist")


def test_service_closes_connection_when_pragma_setup_fails(tmp_path, monkeypatch):
    real_connect = sqlite3.connect
    opened = []

    class FailingSetupConnection(sqlite3.Connection):
        was_closed = False

        def execute(self, sql, *args, **kwargs):
            if "foreign_keys" in sql:
                raise sqlite3.OperationalError("pragma failed")
            return super().execute(sql, *args, **kwargs)

        def close(self):
            self.was_closed = True
            super().close()

    def tracking_connect(*args, **kwargs):
        connection = real_connect(*args, **kwargs, factory=FailingSetupConnection)
        opened.append(connection)
        return connection

    monkeypatch.setattr("gaintt.service.sqlite3.connect", tracking_connect)

    with pytest.raises(sqlite3.OperationalError, match="pragma failed"):
        PlanService(tmp_path / "setup-error.sqlite").initialize()

    assert opened[0].was_closed
