"""Helper invoked as a separate OS process by test_notifications.py.

Applies one Turn to the given Plan and exits. Used to prove that a Honker
publish from an independent process is observed by the test process listening
on the same SQLite file (ticket 16's "second process" acceptance criterion).
"""

from __future__ import annotations

import sys

from kanbain.service import PlanService


def main() -> None:
    db_path, plan_id, base_version = sys.argv[1], sys.argv[2], int(sys.argv[3])
    service = PlanService(db_path)
    service.apply_turn(plan_id, base_version, [{"type": "reassign", "task_id": "a", "assignee": "SUBPROCESS"}])


if __name__ == "__main__":
    main()
