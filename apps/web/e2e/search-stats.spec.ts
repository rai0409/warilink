import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

async function addExpense(page: Page, title: string, amount: string, category: string, date: string) {
  await openTab(page, "支出を追加");
  await page.getByLabel("内容").fill(title);
  await page.getByLabel("金額(円)").fill(amount);
  await page.getByLabel("カテゴリ(任意)").selectOption({ label: category });
  await page.getByLabel("日付(任意)").fill(date);
  await page.getByRole("button", { name: "この内容で登録" }).click();
  await expect(page.getByRole("list", { name: "支出" })).toContainText(title);
}

test("カテゴリ付きで支出を登録し、検索・絞り込みと、立替額と負担額を分けた集計ができる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "集計テスト", "Bさん, Cさん");
  await addExpense(a, "居酒屋", "9,001", "飲み会", "2026-09-20");
  await addExpense(a, "新幹線", "30,000", "交通", "2026-10-01");
  await addExpense(a, "コンビニ", "600", "未分類", "2026-10-02");

  // 検索
  await openTab(a, "支出");
  const meta = a.getByRole("status").filter({ hasText: "件" });
  await expect(meta).toHaveText("3件 · ¥39,601");
  const results = a.getByRole("list", { name: "検索結果" });
  await a.getByLabel("内容で検索").fill("居酒");
  await expect(results.getByRole("listitem")).toHaveCount(1);
  await expect(results).toContainText("居酒屋");
  await a.getByLabel("内容で検索").fill("");
  await expect(results.getByRole("listitem")).toHaveCount(3);

  // 絞り込み(下から出るシート)
  await a.getByRole("button", { name: "カテゴリ" }).click();
  const sheet = a.getByRole("dialog", { name: "絞り込み" });
  await sheet.getByLabel("カテゴリ").selectOption({ label: "未分類" });
  await sheet.getByRole("button", { name: "適用する" }).click();
  await expect(results.getByRole("listitem")).toHaveCount(1);
  await expect(results).toContainText("コンビニ");
  await a.getByRole("button", { name: "未分類" }).click();
  await sheet.getByRole("button", { name: "リセット" }).click();
  await sheet.getByLabel("この日から").fill("2026-10-01");
  await sheet.getByRole("button", { name: "Aさん" }).click();
  await sheet.getByRole("button", { name: "適用する" }).click();
  await expect(meta).toHaveText("2件 · ¥30,600");
  await a.getByLabel("並び順").selectOption({ label: "金額の大きい順" });
  await expect(results.getByRole("listitem").first()).toContainText("新幹線");
  let axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(axe.violations).toEqual([]);

  // 集計: 立て替えた額と負担額を別の列で
  await openTab(a, "集計");
  await expect(a.getByText("¥39,601").first()).toBeVisible();
  const table = a.getByRole("table", { name: "メンバー別の集計" });
  await expect(table.getByRole("row", { name: /Aさん/ })).toContainText("¥39,601");
  await expect(table.getByRole("row", { name: /Bさん/ })).toContainText("¥0");
  const cats = a.getByRole("list", { name: "カテゴリ別の集計" });
  await expect(cats).toContainText("交通");
  await expect(cats).toContainText("未分類");
  await a.getByRole("button", { name: "月別の集計" }).click();
  await expect(a.getByRole("list", { name: "月別の集計" })).toContainText("2026年09月");
  axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(axe.violations).toEqual([]);
});
