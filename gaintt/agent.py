"""Agent orchestration with a deterministic demo model and optional OpenRouter model."""

from __future__ import annotations

import inspect
import json
import os
import re
from datetime import date, timedelta
from typing import Any, Awaitable, Callable, Dict, List, Optional

import httpx

from .domain import DomainValidationError
from .mcp_tools import InMemoryMCPClient, MCPToolCallError, build_fastmcp
from .service import PlanService, StalePlanError

MODEL_ID = "openai/gpt-4.1-mini"
PROVIDER_CONFIG = {
    "order": ["OpenAI"],
    "allow_fallbacks": False,
    "require_parameters": True,
}


class AgentService:
    def __init__(self, service: PlanService, model: Optional[Callable[..., Any]] = None) -> None:
        self.service = service
        self.model = model
        self.client = InMemoryMCPClient(build_fastmcp(service, include_write=True))

    def system_prompt(self, plan: Dict[str, Any]) -> str:
        snapshot = {
            "id": plan["id"],
            "version": plan["version"],
            "plan_start": plan["plan_start"],
            "tasks": [
                {
                    "id": task["id"],
                    "name": task["name"],
                    "assignee": task["assignee"],
                    "start": task["start"],
                    "last_day": task["last_day"],
                    "predecessors": task["predecessors"],
                }
                for task in plan["tasks"]
            ],
        }
        return (
            "You edit exactly one Plan. Address Tasks by id, use only apply_turn, and never invent ids. "
            "If a name is ambiguous, ask the user to choose. Return JSON with mutations and a short reply.\n"
            + json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        )

    async def _emit_stage(self, callback: Optional[Callable[[str], Awaitable[None]]], stage: str) -> None:
        if callback is not None:
            await callback(stage)

    def _with_turns(self, plan: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(plan)
        result["turns"] = self.service.list_turns(plan["id"])
        return result

    async def _openrouter(self, prompt: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
        api_key = os.getenv("OPENROUTER_API_KEY")
        if not api_key:
            return {"mutations": [], "reply": "Опишите правку плана, например: «перенеси релиз на неделю»."}
        messages = [{"role": "system", "content": prompt}, *history]
        async with httpx.AsyncClient(timeout=45) as client:
            response = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                json={
                    "model": MODEL_ID,
                    "messages": messages,
                    "provider": PROVIDER_CONFIG,
                    "response_format": {"type": "json_object"},
                },
            )
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
        return json.loads(content)

    def _resolve(self, plan: Dict[str, Any], query: str) -> Any:
        needle = query.strip(" .«»\"'").casefold()
        candidates = [task for task in plan["tasks"] if needle in task["name"].casefold()]
        if len(candidates) == 1:
            return candidates[0]
        return candidates

    def _deterministic_request(self, plan: Dict[str, Any], message: str) -> Dict[str, Any]:
        normalized = message.strip()
        if normalized.casefold().startswith("а теперь"):
            history = self.service.chat_history(plan["id"], limit=4)
            previous = next((item["content"] for item in reversed(history) if item["role"] == "user"), "")
            normalized = normalized.replace("А теперь", "", 1).strip() or previous
        add_task = re.search(r"добавь(?:\s+новую)?\s+задачу\s+(.+?)\s+после\s+(.+)$", normalized, re.I)
        if add_task:
            predecessor = self._resolve(plan, add_task.group(2).strip(" .—-"))
            if isinstance(predecessor, list):
                return {"clarification": True, "candidates": predecessor}
            name = add_task.group(1).strip(" .—-")
            return {
                "mutations": [
                    {
                        "type": "add_task",
                        "name": name,
                        "duration": 1,
                        "predecessor_ids": [predecessor["id"]],
                    }
                ],
                "reply": f"Добавлена задача «{name}» после «{predecessor['name']}».",
            }
        set_dependency = re.search(r"сделай\s+(.+?)\s+после\s+(.+)$", normalized, re.I)
        if set_dependency:
            task = self._resolve(plan, set_dependency.group(1).strip(" .—-"))
            predecessor = self._resolve(plan, set_dependency.group(2).strip(" .—-"))
            if isinstance(task, list):
                return {"clarification": True, "candidates": task}
            if isinstance(predecessor, list):
                return {"clarification": True, "candidates": predecessor}
            return {
                "mutations": [{"type": "set_predecessors", "task_id": task["id"], "predecessor_ids": [predecessor["id"]]}],
                "reply": f"«{task['name']}» теперь начинается после «{predecessor['name']}».",
            }
        match = re.search(r"перенеси(?:\s+задачу)?\s+[«\"']?(.+?)[»\"']?\s+на\s+(\d+)\s+дн", normalized, re.I)
        if not match:
            match = re.search(r"перенеси(?:\s+задачу)?\s+[«\"']?(.+?)[»\"']?\s+на\s+недел", normalized, re.I)
        if match:
            query = match.group(1).strip()
            days = int(match.group(2)) if match.lastindex and match.lastindex >= 2 and match.group(2).isdigit() else 7
            target = self._resolve(plan, query)
            if isinstance(target, list):
                return {"clarification": True, "candidates": target}
            schedule = next(item for item in plan["tasks"] if item["id"] == target["id"])
            new_start = date.fromisoformat(schedule["start"]) + timedelta(days=days)
            return {
                "mutations": [{"type": "pin_start", "task_id": target["id"], "date": new_start.isoformat()}],
                "reply": f"Задача «{target['name']}» перенесена на {days} дн.",
            }

        bulk = re.search(r"вс[её](?:\s+задачи)?\s+после\s+[«\"']?(.+?)[»\"']?\s+на\s+недел", normalized, re.I)
        if bulk:
            target = self._resolve(plan, bulk.group(1).strip(" .—-"))
            if isinstance(target, list):
                return {"clarification": True, "candidates": target}
            descendants = set()
            changed = True
            while changed:
                changed = False
                for task in plan["tasks"]:
                    if target["id"] in task["predecessors"] or any(item in descendants for item in task["predecessors"]):
                        if task["id"] not in descendants:
                            descendants.add(task["id"])
                            changed = True
            mutations = [
                {
                    "type": "pin_start",
                    "task_id": task["id"],
                    "date": (date.fromisoformat(task["start"]) + timedelta(days=7)).isoformat(),
                }
                for task in plan["tasks"]
                if task["id"] in descendants
            ]
            return {"mutations": mutations, "reply": f"Сдвинуто задач: {len(mutations)}."}

        reassignment = re.search(r"задач[аи]\s+(.+?)\s+на\s+(.+)$", normalized, re.I)
        if reassignment and any(token in normalized.casefold() for token in ("раскидай", "перераспредели")):
            source = reassignment.group(1).strip()
            assignees = [item.strip() for item in re.split(r"\s+и\s+|,", reassignment.group(2)) if item.strip()]
            targets = [task for task in plan["tasks"] if task["assignee"].casefold() == source.casefold()]
            return {
                "mutations": [
                    {"type": "reassign", "task_id": task["id"], "assignee": assignees[index % len(assignees)]}
                    for index, task in enumerate(targets)
                ],
                "reply": f"Перераспределено задач: {len(targets)}.",
            }
        return {"mutations": [], "reply": "Не нашёл однозначную правку. Укажите название задачи и сдвиг."}

    async def _request(self, plan: Dict[str, Any], message: str, history: List[Dict[str, str]]) -> Dict[str, Any]:
        if self.model is not None:
            result = self.model(self.system_prompt(plan), history + [{"role": "user", "content": message}])
            if inspect.isawaitable(result):
                result = await result
            return result
        if os.getenv("OPENROUTER_API_KEY"):
            return await self._openrouter(self.system_prompt(plan), history + [{"role": "user", "content": message}])
        return self._deterministic_request(plan, message)

    async def handle(
        self,
        plan_id: str,
        message: str,
        stage_callback: Optional[Callable[[str], Awaitable[None]]] = None,
    ) -> Dict[str, Any]:
        history = self.service.chat_history(plan_id)
        self.service.record_chat(plan_id, "user", message)
        stages = ["анализирую", "проверяю", "применяю"]
        await self._emit_stage(stage_callback, stages[0])
        last_error = ""
        last_error_code = ""
        attempts = 0
        for attempt in range(1, 4):
            attempts = attempt
            plan = await self.client.call_tool("get_plan", {"plan_id": plan_id})
            retry_message = (
                message if not last_error or last_error_code == StalePlanError.code else f"Исправь мутации после ошибки: {last_error}"
            )
            request = await self._request(plan, retry_message, history)
            await self._emit_stage(stage_callback, stages[1])
            if request.get("clarification"):
                reply = "Нашёл несколько задач. Выберите одну:"
                self.service.record_chat(plan_id, "assistant", reply)
                return {"plan": self._with_turns(plan), "stages": stages[:2], "changes": [], "reply": reply, **request}
            try:
                await self._emit_stage(stage_callback, stages[2])
                result = await self.client.call_tool(
                    "apply_turn",
                    {"plan_id": plan_id, "base_version": plan["version"], "mutations": request.get("mutations", [])},
                )
                reply = request.get("reply", "Ход применён.")
                self.service.record_chat(plan_id, "assistant", reply)
                result["plan"] = self._with_turns(result["plan"])
                return {"stages": stages, "attempts": attempt, "reply": reply, **result}
            except (DomainValidationError, RuntimeError) as exc:
                last_error = str(exc)
                last_error_code = exc.code if isinstance(exc, (DomainValidationError, MCPToolCallError)) else ""
                is_stale = last_error_code == StalePlanError.code
                if (self.model is None and not os.getenv("OPENROUTER_API_KEY") and not is_stale) or attempt == 3:
                    break
        reply = f"Не смог применить ход после трёх попыток: {last_error}"
        self.service.record_chat(plan_id, "assistant", reply)
        current = await self.client.call_tool("get_plan", {"plan_id": plan_id})
        return {
            "plan": self._with_turns(current),
            "stages": stages[:2],
            "attempts": attempts,
            "changes": [],
            "reply": reply,
            "error": last_error,
        }
