import { expect, type Browser, type Locator, type Page } from "@playwright/test";

export async function newUser(browser: Browser, contextOptions = {}): Promise<Page> {
  const ctx = await browser.newContext(contextOptions);
  return ctx.newPage();
}

/** ホーム → グループを作る。others は「Bさん, Cさん」の形 */
export async function createGroup(page: Page, title: string, others: string) {
  await page.goto("/");
  await page.getByRole("link", { name: "グループを作る" }).click();
  await page.getByRole("textbox", { name: "グループ名" }).fill(title);
  await page.getByRole("textbox", { name: "あなたの名前" }).fill("Aさん");
  const names = others.split(",").map((s) => s.trim()).filter(Boolean);
  for (const [i, n] of names.entries()) {
    if (i >= 2) await page.getByRole("button", { name: "メンバーを追加" }).click();
    await page.getByLabel(`メンバー${i + 1}`, { exact: true }).fill(n);
  }
  await page.getByRole("button", { name: "作成する" }).click();
  await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
}

/** いまのページの URL からグループのホーム(/g/xxx)を求める */
export function groupHome(page: Page): string {
  const m = new URL(page.url()).pathname.match(/^\/g\/[^/]+/);
  if (!m) throw new Error(`グループの画面ではありません: ${page.url()}`);
  return m[0];
}

/** グループのホームから画面を開く(「支出を追加」は下の主ボタン) */
export async function openTab(page: Page, name: string) {
  const home = groupHome(page);
  if (new URL(page.url()).pathname !== home) await page.goto(home);
  if (name === "支出を追加") await page.getByRole("link", { name: "支出を追加" }).click();
  else await page.getByRole("list", { name: "グループのメニュー" }).getByRole("link", { name: new RegExp(`^${name}`) }).click();
}

/** 作成者が招待URLを発行して返す */
export async function createInvite(page: Page): Promise<string> {
  await openTab(page, "招待リンク");
  await page.getByRole("button", { name: "招待URLを作成" }).click();
  const url = await page.getByLabel("招待URL").inputValue();
  await page.goto(groupHome(page));
  return url;
}

/** 招待URLから名前を選んで参加する */
export async function joinAs(page: Page, inviteUrl: string, name: string, title: string) {
  await page.goto(inviteUrl);
  await page.getByLabel(name, { exact: true }).check();
  await page.getByRole("button", { name: `${name}として参加` }).click();
  await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
}

/** 均等割りの支出を登録する(登録後はグループのホームに戻る) */
export async function addExpense(page: Page, title: string, amount: string) {
  await openTab(page, "支出を追加");
  await page.getByLabel("内容").fill(title);
  await page.getByLabel("金額(円)").fill(amount);
  await expect(page.getByRole("list", { name: "負担額のプレビュー" })).toBeVisible();
  await page.getByRole("button", { name: "この内容で登録" }).click();
  await expect(page.getByRole("list", { name: "支出" })).toContainText(title);
}

/** 一覧の金額(¥1,234 / −¥240)を合計する */
export async function sumOf(list: Locator): Promise<number> {
  const texts = await list.locator(".amount").allTextContents();
  return texts.reduce((s, t) => s + (t.includes("−") ? -1 : 1) * Number(t.replace(/[^\d]/g, "")), 0);
}

/** レシートの商品行(押すと商品の編集を開く)。名前は「商品名 を編集 1点 · …」 */
export function itemButton(page: Page, name: string): Locator {
  return page.getByRole("button", { name: new RegExp(`^${name}\\s?を編集`) });
}
