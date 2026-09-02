import { expect, test, type Page } from "@playwright/test";
import fs from "node:fs";
import path from "node:path";

const liveUrl = process.env.GAINTT_LIVE_DEMO_URL;
const videoDir = process.env.GAINTT_DEMO_VIDEO_DIR ?? path.resolve("test-results/live-demo");

async function addClientLabel(page: Page, text: string, accent: string) {
  await page.evaluate(
    ({ label, color }) => {
      const element = document.createElement("div");
      element.dataset.demoClientLabel = "true";
      element.textContent = label;
      Object.assign(element.style, {
        position: "fixed",
        zIndex: "2147483647",
        top: "14px",
        left: "50%",
        transform: "translateX(-50%)",
        padding: "10px 18px",
        borderRadius: "999px",
        background: color,
        color: "white",
        font: "700 15px system-ui, sans-serif",
        boxShadow: "0 8px 24px rgba(0,0,0,.22)",
        letterSpacing: ".02em",
      });
      document.body.append(element);
    },
    { label: text, color: accent },
  );
}

async function updateClientLabel(page: Page, text: string, accent: string) {
  await page.evaluate(
    ({ label, color }) => {
      const element = document.querySelector<HTMLElement>("[data-demo-client-label]");
      if (!element) throw new Error("Demo client label is missing");
      element.textContent = label;
      element.style.background = color;
    },
    { label: text, color: accent },
  );
}

test("live demo shows the agent edit arriving in an idle second client", async ({ browser }) => {
  test.skip(!liveUrl, "Set GAINTT_LIVE_DEMO_URL to record the paid live demo");
  test.setTimeout(120_000);
  fs.mkdirSync(videoDir, { recursive: true });

  const recording = { dir: videoDir, size: { width: 1200, height: 720 } };
  const clientA = await browser.newContext({ viewport: recording.size, recordVideo: recording });
  const clientB = await browser.newContext({ viewport: recording.size, recordVideo: recording });
  const pageA = await clientA.newPage();
  const pageB = await clientB.newPage();
  const videoA = pageA.video();
  const videoB = pageB.video();

  try {
    await pageA.goto(liveUrl!);
    await expect(pageA.getByText("РАБОЧИЙ ПЛАН / версия 1", { exact: true })).toBeVisible();
    const capabilityUrl = pageA.url();

    await pageB.goto(capabilityUrl);
    await expect(pageB.getByText("РАБОЧИЙ ПЛАН / версия 1", { exact: true })).toBeVisible();
    await expect(pageB.getByText("2 участника в этом плане", { exact: false })).toBeVisible();
    await addClientLabel(pageA, "КЛИЕНТ A · отправляет команду агенту", "#48617a");
    await addClientLabel(pageB, "КЛИЕНТ B · версия 1 · ждёт live update", "#8a6742");

    const taskB = pageB.locator(".task-index-row").filter({ hasText: "Сформулировать гипотезу" });
    await expect(taskB).toContainText("1 сент");
    await pageA.getByLabel("Сообщение агенту").scrollIntoViewIfNeeded();
    await pageB.getByText("РАБОЧИЙ ПЛАН / версия 1", { exact: true }).scrollIntoViewIfNeeded();
    await pageA.waitForTimeout(2200);

    await pageA
      .getByLabel("Сообщение агенту")
      .fill("Перенеси задачу «Сформулировать гипотезу» на 10 сентября 2026 года.");
    await pageA.waitForTimeout(900);
    await pageA.getByRole("button", { name: "↑", exact: true }).click();

    await expect(pageA.getByText("Задача «Сформулировать гипотезу» перенесена", { exact: false })).toBeVisible({
      timeout: 60_000,
    });
    await expect(pageA.getByText("РАБОЧИЙ ПЛАН / версия 2", { exact: true })).toBeVisible();
    await updateClientLabel(pageA, "КЛИЕНТ A · агент применил правку · версия 2", "#48617a");

    await expect(pageB.getByText("План обновлён другим участником.", { exact: true })).toBeVisible({ timeout: 15_000 });
    await expect(pageB.getByText("РАБОЧИЙ ПЛАН / версия 2", { exact: true })).toBeVisible();
    await expect(taskB).toContainText("10 сент");
    await updateClientLabel(pageB, "КЛИЕНТ B · LIVE UPDATE ПОЛУЧЕН · версия 2", "#2f7a50");
    await pageA.waitForTimeout(3200);
  } finally {
    await clientA.close();
    await clientB.close();
  }

  console.log(`CLIENT_A_VIDEO=${await videoA?.path()}`);
  console.log(`CLIENT_B_VIDEO=${await videoB?.path()}`);
});
