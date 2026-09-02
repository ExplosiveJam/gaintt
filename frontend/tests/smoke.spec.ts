import { expect, test, type Page } from "@playwright/test";
import path from "node:path";

async function demoPause(page: Page) {
  if (process.env.GAINTT_RECORD_DEMO === "1") await page.waitForTimeout(700);
}

async function demoCaption(page: Page, text: string, color = "#48617a") {
  if (process.env.GAINTT_RECORD_DEMO !== "1") return;
  await page.evaluate(
    ({ label, background }) => {
      let element = document.querySelector<HTMLElement>("[data-demo-caption]");
      if (!element) {
        element = document.createElement("div");
        element.dataset.demoCaption = "true";
        Object.assign(element.style, {
          position: "fixed",
          zIndex: "2147483647",
          top: "14px",
          left: "50%",
          transform: "translateX(-50%)",
          padding: "10px 18px",
          borderRadius: "999px",
          color: "white",
          font: "700 15px system-ui, sans-serif",
          boxShadow: "0 8px 24px rgba(0,0,0,.22)",
        });
        document.body.append(element);
      }
      element.textContent = label;
      element.style.background = background;
    },
    { label: text, background: color },
  );
  await demoPause(page);
}

test("Excel → chat → export keeps the edited plan visible", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "Диаграмма Гантта" })).toBeVisible();
  await expect(page.getByText("Название задачи", { exact: true })).toBeVisible();
  await expect(page.getByText("Дата начала", { exact: true })).toBeVisible();
  await expect(page.getByText("Длительность", { exact: true }).first()).toBeVisible();
  await demoCaption(page, "ШАГ 1 · загружаем Excel");

  await page.locator('input[type="file"]').setInputFiles(path.join(process.cwd(), "../examples/gaintt-example.xlsx"));
  await expect(page.locator(".alert").getByText("Загружено 3 задач из 3")).toBeVisible();
  await expect(page.getByText("Исследование").first()).toBeVisible();
  await demoCaption(page, "✓ EXCEL ЗАГРУЖЕН · 3 задачи", "#2f7a50");
  const demoBar = page.locator(".gantt-wrap .wx-bar").filter({ hasText: "Демо" });
  const initialLeft = await demoBar.evaluate((element) => getComputedStyle(element).left);

  await demoCaption(page, "ШАГ 2 · правим план через чат");
  await page.getByLabel("Сообщение агенту").fill("Перенеси задачу Демо на неделю");
  await page.getByRole("button", { name: "↑" }).click();
  await expect(page.getByText("Задача «Демо» перенесена", { exact: false })).toBeVisible();
  await expect(page.locator(".task-index-row").filter({ hasText: "Демо" })).toContainText("13 сент");
  await expect.poll(() => demoBar.evaluate((element) => getComputedStyle(element).left)).not.toBe(initialLeft);
  await expect(page.getByRole("button", { name: "Откатить ход" })).toBeEnabled();
  await demoCaption(page, "✓ АГЕНТ ПРИМЕНИЛ ПРАВКУ", "#2f7a50");

  const download = page.waitForEvent("download");
  await page.getByRole("button", { name: "Выгрузить Excel" }).click();
  expect((await download).suggestedFilename()).toBe("gaintt-plan.xlsx");
  await demoCaption(page, "✓ EXCEL ЭКСПОРТИРОВАН · gaintt-plan.xlsx", "#2f7a50");
});

test("dragging a visible Gantt bar persists the library day diff through apply_turn", async ({ page }) => {
  await page.goto("/");
  await page.locator('input[type="file"]').setInputFiles(path.join(process.cwd(), "../examples/gaintt-example.xlsx"));
  await expect(page.locator(".alert").getByText("Загружено 3 задач из 3")).toBeVisible();

  const researchBar = page.locator(".gantt-wrap .wx-bar").filter({ hasText: "Исследование" });
  const box = await researchBar.boundingBox();
  if (!box) throw new Error("Gantt bar is not visible");
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
  await page.mouse.down();
  await page.mouse.move(box.x + box.width / 2 + 100, box.y + box.height / 2, { steps: 8 });
  await page.mouse.up();

  await expect(page.locator(".task-index-row").filter({ hasText: "Исследование" })).toContainText("2 сент");
});
