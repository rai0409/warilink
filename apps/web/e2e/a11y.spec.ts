import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// WCAG 2.x A/AA の自動検査(axe-core)。自動検査で見つからない問題もあるため、手動確認の代わりにはならない。

async function audit(page: Page, label: string) {
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const summary = r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => n.target.join(" ")).join(", ")}`);
  expect(summary, `${label} のアクセシビリティ違反`).toEqual([]);
}

test("主要画面に WCAG A/AA の自動検出違反がない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await a.goto("/");
  await audit(a, "開始画面");
  await createGroup(a, "検査用", "Bさん");
  await audit(a, "概要");

  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("ランチ");
  await a.getByLabel("金額(円)").fill("2000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toBeVisible();
  await audit(a, "支出入力");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("ランチ");

  await openTab(a, "レシート");
  await a.getByLabel("店名・内容").fill("店");
  await a.getByLabel("レシートの合計金額(円)").fill("999");
  await a.getByRole("group", { name: "商品1" }).getByLabel("金額(円)").fill("1000");
  await expect(a.getByRole("region", { name: "合計金額の不一致" })).toBeVisible();
  await audit(a, "レシート(不一致表示)");

  await openTab(a, "精算");
  await expect(a.getByText("最小送金回数")).toBeVisible();
  await audit(a, "精算");
  await openTab(a, "設定");
  await audit(a, "設定");
  await a.goto("/pricing");
  await expect(a.getByRole("table", { name: "料金プランの比較" })).toBeVisible();
  await audit(a, "料金プラン");
});
