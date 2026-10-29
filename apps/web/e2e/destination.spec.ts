import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { addExpense, createGroup, createInvite, groupHome, joinAs, newUser, openTab } from "./helpers";

// 受取人が Passkey 登録 → 振込先を暗号化保存・共有 → 支払者だけに表示 → 受取人が変更すると支払者に警告。
// 送金は実行しない(表示とコピーのみ)。口座情報は架空の値。

async function passkeyDevice(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "internal", hasResidentKey: true, hasUserVerification: true,
      isUserVerified: true, automaticPresenceSimulation: true },
  });
}

async function fillBank(page: Page, number: string, branch: string) {
  await page.getByLabel("銀行コード(4桁)").fill("0001");
  await page.getByLabel("銀行名").fill("テスト銀行");
  await page.getByLabel("支店コード(3桁)").fill("123");
  await page.getByLabel("支店名").fill(branch);
  await page.getByLabel("口座番号(7桁)").fill(number);
  await page.getByLabel("口座名義(カタカナ)").fill("テスト タロウ");
}

test("受取人が共有した振込先を、支払者だけが確認でき、変更されると警告が出る", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "口座テスト", "Bさん");
  const home = groupHome(a);
  const inviteUrl = await createInvite(a);

  // A: Passkey 登録 → 振込先を保存(共有を許可)
  await passkeyDevice(a);
  await a.goto("/account");
  await a.getByRole("button", { name: "Passkey を登録する" }).click();
  await expect(a.getByText("Passkey 登録済み")).toBeVisible();
  await a.reload();
  await fillBank(a, "1234567", "本店");
  await a.getByRole("button", { name: "保存する" }).click();
  await expect(a.getByRole("status").filter({ hasText: "振込先を登録しました" })).toBeVisible();
  await expect(a.getByText("テスト銀行 本店 普通 ***4567")).toBeVisible();
  await expect(a.getByText("1234567")).toHaveCount(0); // 一覧は伏せ字
  const axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(axe.violations).toEqual([]);

  // B が参加。A が立替、精算を確定
  const b = await newUser(browser, { baseURL });
  await joinAs(b, inviteUrl, "Bさん", "口座テスト");
  await a.goto(home);
  await addExpense(a, "宿", "8,000");
  await openTab(a, "精算");
  await a.getByRole("button", { name: "精算案を確定" }).click();
  // 受取人 A 自身の画面には振込先の表示ボタンは出ない
  await a.getByRole("list", { name: "確定した精算" }).getByRole("link").first().click();
  await expect(a.getByRole("heading", { name: "受け取りの確認" })).toBeVisible();
  await expect(a.getByRole("button", { name: "振込先を表示" })).toHaveCount(0);

  // B(支払者)だけが表示できる
  await b.goto(`${home}/settle`);
  await b.getByRole("list", { name: "確定した精算" }).getByRole("link").filter({ hasText: "あなた" }).click();
  await expect(b.getByRole("heading", { name: "送金の記録" })).toBeVisible();
  const transferUrl = b.url();
  await b.getByRole("button", { name: "振込先を表示" }).click();
  const view = b.getByLabel("振込先", { exact: true });
  await expect(view).toContainText("1234567");
  await expect(view).toContainText("テスト タロウ");
  await expect(view).toContainText("照合コード");
  await expect(view).toContainText("送金を実行しません");

  // A が振込先を変更 → B に警告
  await a.goto("/account");
  await a.getByRole("button", { name: "変更する" }).click();
  await fillBank(a, "7654321", "駅前");
  await a.getByRole("button", { name: "この内容で変更" }).click();
  await expect(a.getByRole("status").filter({ hasText: "振込先を変更しました" })).toBeVisible();
  await b.goto(transferUrl);
  await b.getByRole("button", { name: "振込先を表示" }).click();
  const warn = b.getByRole("alert").filter({ hasText: /\S/ });
  await expect(warn).toContainText("受取人が振込先を変更しました");
  await expect(warn).toContainText("***4567");
  await expect(b.getByLabel("振込先", { exact: true })).toContainText("7654321");

  // 共有を取り消すと表示されない
  await a.reload();
  await a.getByLabel("あなたに支払う精算の相手に表示する").click(); // 保存してから表示が切り替わる
  await expect(a.getByRole("status").filter({ hasText: "取り消しました" })).toBeVisible();
  await b.reload();
  await b.getByRole("button", { name: "振込先を表示" }).click();
  await expect(b.getByText("受取人は振込先を共有していません")).toBeVisible();

  // 削除は元に戻せないことを確認してから行う。キャンセルすれば何も変わらない
  await a.reload();
  await a.getByRole("button", { name: "削除する", exact: true }).click();
  const dialog = a.getByRole("dialog", { name: "振込先を削除しますか?" });
  await expect(dialog).toContainText("元に戻せません");
  await dialog.getByRole("button", { name: "キャンセル" }).click();
  await expect(a.getByText(/\*\*\*4321/)).toBeVisible();
  await a.getByRole("button", { name: "削除する", exact: true }).click();
  await dialog.getByRole("button", { name: "削除する" }).click();
  await expect(a.getByRole("status").filter({ hasText: "振込先を削除しました" })).toBeVisible();
});
