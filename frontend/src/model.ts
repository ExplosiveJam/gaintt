export type Link = { id: string; source: string; target: string; type: "e2s" };

export type Task = {
  id: string;
  name: string;
  description: string;
  assignee: string;
  duration: number;
  predecessors: string[];
  pinned_start: string | null;
  due_date: string | null;
  start: string;
  finish: string;
  last_day: string;
  overdue: boolean;
};

export type Plan = {
  id: string;
  name: string;
  plan_start: string;
  version: number;
  tasks: Task[];
  links: Link[];
  turns?: Turn[];
  member_count?: number;
};

export type Turn = {
  id: string;
  version: number;
  changes: { label: string }[];
  can_revert: boolean;
};

export type GanttTask = {
  id: string;
  text: string;
  start: Date;
  end: Date;
  duration: number;
  parent: number;
  css?: string;
};

export function toGanttTasks(plan: Plan): GanttTask[] {
  return plan.tasks.map((task) => ({
    id: task.id,
    text: task.name,
    start: new Date(`${task.start}T00:00:00`),
    end: new Date(`${task.finish}T00:00:00`),
    duration: task.duration,
    parent: 0,
    css: isOverdue(task) ? "task-overdue" : ""
  }));
}

export function toGanttLinks(plan: Plan): Link[] {
  return plan.links.map((link) => ({ ...link, type: "e2s" as const }));
}

export function dragDiffToDate(start: string, days: number): string {
  const date = new Date(`${start}T00:00:00Z`);
  date.setUTCDate(date.getUTCDate() + days);
  return date.toISOString().slice(0, 10);
}

export function isOverdue(task: Task): boolean {
  return Boolean(task.due_date && task.last_day > task.due_date);
}

export function taskById(plan: Plan | null, taskId: string | null): Task | undefined {
  return plan?.tasks.find((task) => task.id === taskId);
}

const PLAN_URL_PATTERN = /^\/plan\/([0-9a-fA-F-]{8,})$/;

/** Capability-URL: the plan_id in the path, if this location names one. */
export function planIdFromPath(pathname: string): string | null {
  return pathname.match(PLAN_URL_PATTERN)?.[1] ?? null;
}

export function planUrl(planId: string): string {
  return `/plan/${planId}`;
}

export function formatDate(value: string | null | undefined): string {
  if (!value) return "—";
  return new Intl.DateTimeFormat("ru-RU", { day: "numeric", month: "short", year: "numeric" }).format(new Date(`${value}T00:00:00`));
}
