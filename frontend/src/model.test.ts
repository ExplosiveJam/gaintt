import { describe, expect, it } from "vitest";
import { dragDiffToDate, isOverdue, planIdFromPath, planUrl, taskById, toGanttLinks, toGanttTasks, type Plan } from "./model";

const plan: Plan = {
  id: "p",
  name: "Plan",
  plan_start: "2026-09-01",
  version: 1,
  tasks: [
    { id: "a", name: "A", description: "", assignee: "I", duration: 1, predecessors: [], pinned_start: null, due_date: null, start: "2026-09-01", finish: "2026-09-02", last_day: "2026-09-01", overdue: false },
    { id: "b", name: "B", description: "", assignee: "P", duration: 2, predecessors: ["a"], pinned_start: null, due_date: "2026-09-02", start: "2026-09-02", finish: "2026-09-04", last_day: "2026-09-03", overdue: true }
  ],
  links: []
};

describe("controlled Gantt adapter", () => {
  it("maps backend schedule to tasks and links without local scheduling", () => {
    const tasks = toGanttTasks(plan);
    expect(tasks[0]).toMatchObject({ id: "a", text: "A", duration: 1 });
    expect(tasks[0].start).toEqual(new Date("2026-09-01T00:00:00"));
    expect(toGanttLinks({ ...plan, links: [{ id: "a->b", source: "a", target: "b", type: "e2s" }] })).toEqual([
      { id: "a->b", source: "a", target: "b", type: "e2s" }
    ]);
  });

  it("converts the Gantt library day diff directly to a calendar date", () => {
    expect(dragDiffToDate("2026-09-01", 2)).toBe("2026-09-03");
  });

  it("marks due date violations", () => {
    expect(isOverdue(plan.tasks[1])).toBe(true);
  });

  it("resolves the selected task from the latest plan snapshot", () => {
    const updated = {
      ...plan,
      version: 2,
      tasks: plan.tasks.map((task) => task.id === "a" ? { ...task, assignee: "Updated" } : task)
    };

    expect(taskById(updated, "a")?.assignee).toBe("Updated");
    expect(taskById(updated, "missing")).toBeUndefined();
  });
});

describe("capability-URL routing", () => {
  it("extracts a plan_id from a /plan/{id} path", () => {
    expect(planIdFromPath("/plan/0f6f1a3a-6e7a-4b6a-9d2e-1f9b6c9d0a11")).toBe("0f6f1a3a-6e7a-4b6a-9d2e-1f9b6c9d0a11");
  });

  it("returns null for paths that don't name a Plan", () => {
    expect(planIdFromPath("/")).toBeNull();
    expect(planIdFromPath("/api/plan")).toBeNull();
  });

  it("builds a shareable URL from a plan id", () => {
    expect(planUrl("abc-123")).toBe("/plan/abc-123");
  });
});
