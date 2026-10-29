import { execFileSync } from "node:child_process";
import { mkdirSync, readFileSync } from "node:fs";
import path from "node:path";
import AxeBuilder from "@axe-core/playwright";
import { expect, test } from "@playwright/test";
import { PNG_TINY } from "./fixtures";
import { createGroup, groupHome, itemButton, newUser, sumOf } from "./helpers";

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
  const home = groupHome(a);
  await a.goto(`${home}/receipt`);
  await expect(a.getByText(/今月の残り: 3枚/)).toBeVisible();
  await a.getByLabel("レシートを撮影・画像を選ぶ").setInputFiles(image);

  // 読み取り待ち → 確認画面(下書きであることを明示)
  await expect(a.getByRole("region", { name: "読み取り結果の確認" })).toBeVisible({ timeout: 90_000 });
  await expect(a.getByText(/読み取った内容は下書きです/)).toBeVisible();
  await expect(a.getByRole("img", { name: "アップロードしたレシート" })).toBeVisible();
  await expect(a.getByLabel("レシートの合計金額(円)")).toHaveValue(String(label.total_minor));
  for (const it of label.items) {
    await itemButton(a, it.name).click();
    const panel = a.getByRole("dialog", { name: "商品" });
    await expect(panel.getByLabel("商品名")).toHaveValue(it.name);
    await expect(panel.getByLabel("金額(円)")).toHaveValue(String(it.line_minor));
    await expect(panel.getByLabel("数量", { exact: true })).toHaveValue(String(it.quantity));
    await panel.getByRole("button", { name: "完了" }).click();
  }

  const axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"])
    .analyze();
  expect(axe.violations.map((v) => v.id), "確認画面のアクセシビリティ違反").toEqual([]);

  // 負担者は自動で決めない: 未割当のままでは登録できない
  await a.getByRole("button", { name: "内訳を確認" }).click();
  const summary = a.getByRole("dialog", { name: "内訳を確認" });
  await expect(summary.getByRole("button", { name: "登録する" })).toBeDisabled();
  await summary.getByRole("button", { name: "戻る" }).click();
  for (const it of label.items) await a.getByLabel(`${it.name}を選択`).check();
  await a.getByRole("button", { name: `選択した${label.items.length}商品を割り当て` }).click();
  await a.getByRole("dialog", { name: "割り当て" }).getByRole("button", { name: `${label.items.length}商品に適用` }).click();
  await a.getByRole("button", { name: "内訳を確認" }).click();
  await expect(summary.getByText("明細の合計がレシートの合計と一致しました。")).toBeVisible();
  await summary.getByRole("button", { name: "登録する" }).click();

  const confirmed = a.getByRole("list", { name: "確定した負担額" });
  await expect(confirmed).toBeVisible();
  expect(await sumOf(confirmed)).toBe(label.total_minor);

  // 枠を1枚消費した
  await a.goto(`${home}/receipt`);
  await expect(a.getByText(/今月の残り: 2枚/)).toBeVisible();
});

test("読み取れない画像は失敗を表示し、枠を返して手入力へ案内する", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "OCR失敗", "Bさん");
  await a.goto(`${groupHome(a)}/receipt`);
  await a.getByLabel("レシートを撮影・画像を選ぶ").setInputFiles({ name: "tiny.png", mimeType: "image/png",
    buffer: PNG_TINY });
  await expect(a.getByRole("heading", { name: "読み取れませんでした" })).toBeVisible({ timeout: 60_000 });
  await expect(a.getByText(/画像が小さすぎます/)).toBeVisible();
  await expect(a.getByText(/返還済み/)).toBeVisible();
  await a.getByRole("link", { name: "手入力で登録する" }).click();
  await expect(a.getByText(/今月の残り: 3枚/)).toBeVisible();
});
