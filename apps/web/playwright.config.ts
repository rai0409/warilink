import { defineConfig, devices } from "@playwright/test";

// E2E 用に開発 DB とは別ポートで API と Web を起動する(既存サービスと衝突させない)
const API_PORT = 18000;
const WEB_PORT = 13000;
// API とワーカーが共有するレシート画像の保存先(E2E 専用)
const RECEIPTS_DIR = "../../var/e2e-receipts";
const DB_URL =
  process.env.E2E_DATABASE_URL ?? "postgresql+psycopg://warilink:warilink-dev-only@localhost:55432/warilink_e2e";

// E2E は DB を書き換える(マイグレーション・データ作成)。ローカルの warilink_e2e(_*) 以外なら起動しない(fail-closed)
{
  const m = DB_URL.match(/^postgresql\+psycopg:\/\/[^@]+@([^:/]+)(?::\d+)?\/([^?]+)/);
  if (!m || !["localhost", "127.0.0.1"].includes(m[1]) || !/^warilink_e2e(_[a-z0-9_]+)?$/.test(m[2])) {
    throw new Error(`E2E は使い捨て DB(ローカルの warilink_e2e / warilink_e2e_*)でだけ実行できます: ${m ? `${m[1]}/${m[2]}` : "形式不明"}`);
  }
}

export default defineConfig({
  testDir: "./e2e",
  timeout: 120_000,
  fullyParallel: false,
  reporter: process.env.CI ? [["list"], ["html", { open: "never" }]] : [["list"]],
  forbidOnly: !!process.env.CI,
  retries: 0, // 失敗を再試行で隠さない
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
        `WARILINK_ALLOWED_ORIGINS='["http://localhost:${WEB_PORT}"]' WARILINK_FEATURE_PASSKEYS=true ` +
        `WARILINK_FEATURE_PAYMENT_DESTINATIONS=true WARILINK_FEATURE_OCR=true ` +
        // Node(Next の中継)の keep-alive 5 秒と uvicorn 既定の 5 秒が同じだと、閉じかけの接続を再利用して
        // ECONNRESET になることがある(2026-10-21 に 4 回中 1 回発生)。サーバー側を長くして競合を避ける
        `../../.venv/bin/uvicorn app.main:app --port ${API_PORT} --timeout-keep-alive 65`,
      url: `http://127.0.0.1:${API_PORT}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 120_000,
    },
    {
      // 本物の PaddleOCR ワーカー(モデル読み込みに十数秒かかる)
      command:
        `cd ../api && WARILINK_DATABASE_URL=${DB_URL} WARILINK_RECEIPT_STORAGE_DIR=${RECEIPTS_DIR} WARILINK_FEATURE_OCR=true ` +
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
