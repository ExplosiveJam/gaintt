import { defineConfig, devices } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 30_000,
  use: {
    baseURL: "http://127.0.0.1:8765",
    trace: "on-first-retry",
    video: process.env.GAINTT_RECORD_DEMO === "1" ? "on" : "retain-on-failure",
    ...devices["Desktop Chrome"],
    launchOptions: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH
      ? { executablePath: process.env.PLAYWRIGHT_CHROMIUM_EXECUTABLE_PATH }
      : undefined
  },
  webServer: {
    command: "cd .. && OPENROUTER_API_KEY= GAINTT_DB_PATH=/tmp/gaintt-e2e.sqlite UV_CACHE_DIR=/tmp/gaintt-uv-cache uv run uvicorn gaintt.main:app --host 127.0.0.1 --port 8765",
    url: "http://127.0.0.1:8765/health",
    reuseExistingServer: false,
    timeout: 30_000
  }
});
