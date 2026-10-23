import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PNG_TINY } from "./fixtures";
import { createGroup, newUser, openTab } from "./helpers";

// 本物の PaddleOCR ワーカーで合成レシートを読み取り、確認・訂正して登録する(外部サービスは使わない)。

type Label = { items: { name: string; quantity: number; line_minor: number }[]; total_minor: number };

function synthReceipt(dir: string, seed: number): { image: string; label: Label } {
  mkdirSync(dir, { recursive: true });
  execFileSync("../../.venv/bin/python", ["-m", "app.ocr.cli", "synth", "--out", dir, "--count", "1", "--seed",
    String(seed)], { cwd: path.resolve(__dirname, "../../api") });
  const stem = path.join(dir, `synth-${seed}-0000`);
  return { image: `${stem}.png`, label: JSON.parse(readFileSync(`${stem}.json`, "utf8")) };
}

test("レシート画像を読み取り、下書きを確認して商品別に登録できる", async ({ browser, baseURL }, info) => {
  const { image, label } = synthReceipt(info.outputPath("synth"), 4242);
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "OCRテスト", "Bさん");
  await openTab(a, "レシート");
  await expect(a.getByText(/今月の残り: 3枚/)).toBeVisible();
  await a.getByLabel("レシートを撮影・画像を選ぶ").setInputFiles(image);

  // 読み取り待ち → 確認画面(下書きであることを明示)
  await expect(a.getByRole("heading", { name: "読み取り結果の確認" })).toBeVisible({ timeout: 90_000 });
  await expect(a.getByText(/読み取った内容は下書きです/)).toBeVisible();
  await expect(a.getByRole("img", { name: "アップロードしたレシート" })).toBeVisible();
  await expect(a.getByLabel("レシートの合計金額(円)")).toHaveValue(String(label.total_minor));
  for (const [n, it] of label.items.entries()) {
    const g = a.getByRole("group", { name: `商品${n + 1}`, exact: true });
    await expect(g.getByLabel("商品名")).toHaveValue(it.name);
    await expect(g.getByLabel("金額(円)")).toHaveValue(String(it.line_minor));
    await expect(g.getByLabel("数量", { exact: true })).toHaveValue(String(it.quantity));
  }

  const axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations.map((v) => v.id), "確認画面のアクセシビリティ違反").toEqual([]);

  // 負担者は自動で決めない: 未割当のままでは登録できない
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeDisabled();
  for (let n = 1; n <= label.items.length; n++) {
    await a.getByRole("group", { name: `商品${n}`, exact: true }).getByLabel("一括割当の対象").check();
  }
  const bulk = a.getByRole("region", { name: "一括割当" });
  await bulk.getByRole("button", { name: "Aさん" }).click();
  await bulk.getByRole("button", { name: "Bさん" }).click();
  await bulk.getByRole("button", { name: "選択した商品に割り当てる" }).click();
  await expect(a.getByText(`明細の合計がレシートの合計 ${label.total_minor.toLocaleString("ja-JP")}円 と一致しました`))
    .toBeVisible();
  await a.getByRole("button", { name: "この内容で登録" }).click();

  const confirmed = a.getByRole("list", { name: "確定した負担額" });
  await expect(confirmed).toBeVisible();
  const amounts = await confirmed.locator(".amount").allTextContents();
  expect(amounts.reduce((s, t) => s + Number(t.replace(/[^\d]/g, "")), 0)).toBe(label.total_minor);

  // 枠を1枚消費し、同じレシートは二度登録できない
  await openTab(a, "レシート");
  await expect(a.getByText(/今月の残り: 2枚/)).toBeVisible();
});

test("読み取れない画像は失敗を表示し、枠を返して手入力へ案内する", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "OCR失敗", "Bさん");
  await openTab(a, "レシート");
  await a.getByLabel("レシートを撮影・画像を選ぶ").setInputFiles({ name: "tiny.png", mimeType: "image/png",
    buffer: PNG_TINY });
  await expect(a.getByRole("heading", { name: "読み取れませんでした" })).toBeVisible({ timeout: 60_000 });
  await expect(a.getByText(/画像が小さすぎます/)).toBeVisible();
  await expect(a.getByText(/返還済み/)).toBeVisible();
  await a.getByRole("link", { name: "手入力で登録する" }).click();
  await expect(a.getByText(/今月の残り: 3枚/)).toBeVisible();
});
