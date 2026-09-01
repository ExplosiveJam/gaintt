import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8000",
    trace: "on-first-retry",
    video: process.env.KANBAIN_RECORD_DEMO === "1" ? "on" : "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined
  },
  webServer: {
    command: "cd .. && OPENROUTER_API_KEY= KANBAIN_DB_PATH=/tmp/kanbain-e2e.sqlite UV_CACHE_DIR=/tmp/kanbain-uv-cache uv run uvicorn kanbain.main:app --host 127.0.0.1 --port 8000",
    url: "http://127.0.0.1:8000/health",
    reuseExistingServer: false,
    timeout: 30_000
  }
});
