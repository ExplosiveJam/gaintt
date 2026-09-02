import pytest

from gaintt.domain import Task
from gaintt.mcp_tools import InMemoryMCPClient, MCPToolRegistry
from gaintt.service import PlanService


@pytest.mark.asyncio
async def test_in_memory_mcp_apply_turn_reaches_application_service(tmp_path):
    service = PlanService(tmp_path / "plans.sqlite")
    service.initialize()
    plan = service.create_plan("owner", tasks={"a": Task(id="a", name="A", duration=1)})
    client = InMemoryMCPClient(MCPToolRegistry(service))

    result = await client.call_tool(
        "apply_turn",
        {"plan_id": plan.id, "base_version": 1, "mutations": [{"type": "reassign", "task_id": "a", "assignee": "X"}]},
    )

    assert result["plan"]["tasks"][0]["assignee"] == "X"
