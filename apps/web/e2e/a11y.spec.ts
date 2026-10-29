import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { addExpense, createGroup, groupHome, itemButton, newUser, openTab } from "./helpers";

// WCAG 2.x A/AA の自動検査(axe-core)。自動検査で見つからない問題もあるため、手動確認の代わりにはならない。

async function audit(page: Page, label: string) {
  // 画面名(document.title)が付くのを待ってから検査する(付く前に検査すると document-title が揺れる)
  await expect(page).toHaveTitle(/.+/);
  const r = await new AxeBuilder({ page }).withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa"]).analyze();
  const summary = r.violations.map((v) => `${v.id}: ${v.nodes.map((n) => `${n.target.join(" ")} ${n.failureSummary}`).join(", ")}`);
  expect(summary, `${label} のアクセシビリティ違反`).toEqual([]);
}

test("主要画面に WCAG A/AA の自動検出違反がない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await a.goto("/");
  await audit(a, "ホーム");
  await a.getByRole("link", { name: "グループを作る" }).click();
  await expect(a.getByLabel("グループ名")).toBeVisible();
  await audit(a, "グループ作成");
  await createGroup(a, "検査用", "Bさん");
  const home = groupHome(a);

  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("ランチ");
  await a.getByLabel("金額(円)").fill("2000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toBeVisible();
  await audit(a, "支出入力");
  await a.getByRole("button", { name: "負担額" }).click();
  await expect(a.getByRole("dialog", { name: "負担額" })).toBeVisible();
  await audit(a, "負担額の設定");
  await a.getByRole("dialog", { name: "負担額" }).getByRole("button", { name: "適用する" }).click();
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("ランチ");
  await audit(a, "グループのホーム");

  await a.goto(`${home}/receipt`);
  await a.getByLabel("店名・内容").fill("店");
  await a.getByLabel("レシートの合計金額(円)").fill("999");
  await itemButton(a, "商品1").click();
  await a.getByRole("dialog", { name: "商品" }).getByLabel("金額(円)").fill("1000");
  await audit(a, "商品の編集");
  await a.getByRole("button", { name: "完了" }).click();
  await a.getByRole("button", { name: "内訳を確認" }).click();
  await expect(a.getByRole("region", { name: "合計金額の不一致" })).toBeVisible();
  await audit(a, "内訳の確認(不一致表示)");

  await addExpense(a, "夕食", "3000");
  await openTab(a, "精算");
  await expect(a.getByRole("heading", { name: "1回の送金で精算" })).toBeVisible();
  await audit(a, "精算案");
  await a.getByRole("button", { name: "精算案を確定" }).click();
  await a.getByRole("list", { name: "確定した精算" }).getByRole("link").first().click();
  await expect(a.getByRole("heading", { name: "受け取りの確認" })).toBeVisible();
  await audit(a, "受け取りの確認");

  for (const [path, label] of [
    ["/members", "メンバー"], ["/invite", "招待リンク"], ["/settings", "グループ設定"], ["/expenses", "支出の検索"], ["/stats", "集計"],
  ]) {
    await a.goto(home + path);
    await expect(a.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(a, label);
  }
  await a.goto(`${home}/settings`);
  await a.getByRole("button", { name: "グループから抜ける" }).click();
  await expect(a.getByRole("dialog")).toBeVisible();
  await audit(a, "確認ダイアログ");

  for (const [path, label] of [["/settings", "設定"], ["/settings/retention", "データの保存期間"], ["/account", "アカウント"]]) {
    await a.goto(path);
    await expect(a.getByRole("heading", { level: 1 })).toBeVisible();
    await audit(a, label);
  }
  await a.goto("/pricing");
  await expect(a.getByRole("table", { name: "料金プランの比較" })).toBeVisible();
  await audit(a, "料金プラン");
});
