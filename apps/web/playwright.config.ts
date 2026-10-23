import { defineConfig, devices } from "@playwright/test";

// E2E 用に開発 DB とは別ポートで API と Web を起動する(既存サービスと衝突させない)
const API_PORT = 18000;
const WEB_PORT = 13000;
// API とワーカーが共有するレシート画像の保存先(E2E 専用)
const RECEIPTS_DIR = "../../var/e2e-receipts";
const DB_URL =
  process.env.E2E_DATABASE_URL ?? "postgresql+psycopg://warilink:warilink-dev-only@localhost:55432/warilink_e2e";

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  fullyParallel: false,
  reporter: [["list"]],
  use: { baseURL: `http://localhost:${WEB_PORT}`, trace: "retain-on-failure" },
  projects: [
    { name: "mobile", use: { ...devices["Pixel 7"] } },
    { name: "desktop", use: { ...devices["Desktop Chrome"] } },
  ],
  webServer: [
    {
      command:
        `cd ../api && WARILINK_DATABASE_URL=${DB_URL} WARILINK_ALLOWED_ORIGINS='["http://localhost:${WEB_PORT}"]' ` +
        `../../.venv/bin/alembic upgrade head && WARILINK_DATABASE_URL=${DB_URL} WARILINK_RECEIPT_STORAGE_DIR=${RECEIPTS_DIR} ` +
        `WARILINK_ALLOWED_ORIGINS='["http://localhost:${WEB_PORT}"]' ` +
        `../../.venv/bin/uvicorn app.main:app --port ${API_PORT}`,
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      // 本物の PaddleOCR ワーカー(モデル読み込みに十数秒かかる)
      command:
        `cd ../api && WARILINK_DATABASE_URL=${DB_URL} WARILINK_RECEIPT_STORAGE_DIR=${RECEIPTS_DIR} ` +
        `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=True ../../.venv/bin/python -m app.ocr.worker`,
      wait: { stderr: /ocr worker \S+ ready/ },
      reuseExistingServer: false,
      timeout: 180_000,
    },
    {
      command: `API_ORIGIN=http://127.0.0.1:${API_PORT} pnpm exec next dev -p ${WEB_PORT}`,
      url: `http://localhost:${WEB_PORT}`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
  ],
});
