import { expect, type Browser, type Page } from "@playwright/test";

export async function newUser(browser: Browser, contextOptions = {}): Promise<Page> {
  const ctx = await browser.newContext(contextOptions);
  return ctx.newPage();
}

export async function createGroup(page: Page, title: string, others: string) {
  await page.goto("/");
  await page.getByLabel("グループ名").fill(title);
  await page.getByLabel("あなたの名前").fill("Aさん");
  await page.getByLabel("ほかのメンバー").fill(others);
  await page.getByRole("button", { name: "グループを作成" }).click();
  await expect(page.getByRole("heading", { name: title, level: 1 })).toBeVisible();
}

export async function openTab(page: Page, name: string) {
  await page.getByRole("navigation", { name: "グループのメニュー" }).getByRole("link", { name }).click();
}
