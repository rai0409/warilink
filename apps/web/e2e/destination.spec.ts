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

test("前の振込先の表示が、別の送金・再訪問・遅れた応答・ログイン状態の変化で残らない", async ({ browser, baseURL }) => {
  // A(受取人・登録済み)と C(名前だけ)が立て替え、B が 2 人へ送金する
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "表示の切り替え", "Bさん, Cさん");
  const home = groupHome(a);
  const inviteUrl = await createInvite(a);
  await passkeyDevice(a);
  await a.goto("/account");
  await a.getByRole("button", { name: "Passkey を登録する" }).click();
  await expect(a.getByText("Passkey 登録済み")).toBeVisible();
  await a.reload();
  await fillBank(a, "1234567", "本店");
  await a.getByRole("button", { name: "保存する" }).click();
  await expect(a.getByRole("status").filter({ hasText: "振込先を登録しました" })).toBeVisible();

  const b = await newUser(browser, { baseURL });
  await joinAs(b, inviteUrl, "Bさん", "表示の切り替え");
  const me = await (await b.request.get("/api/v1/groups/" + home.split("/")[2])).json();
  const id = (name: string) => me.members.find((m: { display_name: string }) => m.display_name === name).id;
  for (const [payer, title] of [["Aさん", "宿"], ["Cさん", "夕食"]] as const) {
    const r = await b.request.post(`/api/v1/groups/${me.public_id}/expenses`, {
      headers: { "X-WariLink-CSRF": "1", "Idempotency-Key": crypto.randomUUID() },
      data: { title, amount_minor: 3000, payers: [{ member_id: id(payer), amount_minor: 3000 }],
        split: { mode: "equal", shares: [{ member_id: id("Bさん"), value: 1 }] } },
    });
    expect(r.status()).toBe(201);
  }
  await b.goto(`${home}/settle`);
  await b.getByRole("button", { name: "精算案を確定" }).click();
  const list = b.getByRole("list", { name: "確定した精算" });
  await expect(list.getByRole("link")).toHaveCount(2);

  // 1) A への送金で振込先を表示 → C への送金へ移ると、A の口座は出ない
  await list.getByRole("link").filter({ hasText: "Aさん" }).click();
  await expect(b).toHaveURL(/\/settle\/[0-9a-f-]{36}$/);
  const toA = b.url();
  await b.getByRole("button", { name: "振込先を表示" }).click();
  await expect(b.getByLabel("振込先", { exact: true })).toContainText("1234567");
  await b.getByRole("link").filter({ hasText: "Cさんへ" }).click();
  await expect(b).not.toHaveURL(toA);
  await expect(b.getByText("1234567")).toHaveCount(0);
  await expect(b.getByRole("button", { name: "振込先を表示" })).toBeVisible();

  // 2) 戻る(再訪問)と、押すまで表示しない
  await b.goBack();
  await expect(b).toHaveURL(toA);
  await expect(b.getByRole("button", { name: "振込先を表示" })).toBeVisible();
  await expect(b.getByText("1234567")).toHaveCount(0);

  // 3) 表示の応答が遅れて、その間に別の送金へ移った: 遅れて届いた A の口座を出さない
  let release: () => void = () => {};
  const held = new Promise<void>((r) => (release = r));
  await b.route("**/destination", async (route) => {
    await held;
    await route.continue();
  });
  await b.getByRole("button", { name: "振込先を表示" }).click();
  await b.getByRole("link").filter({ hasText: "Cさんへ" }).click();
  await expect(b).not.toHaveURL(toA);
  release();
  await b.waitForTimeout(1_000);
  await expect(b.getByText("1234567")).toHaveCount(0);
  await b.unroute("**/destination");

  // 4) 表示中にセッションが切れた(別のタブでのログアウトなど): 画面に戻ったら表示を閉じる
  await b.goto(toA);
  await b.getByRole("button", { name: "振込先を表示" }).click();
  await expect(b.getByLabel("振込先", { exact: true })).toContainText("1234567");
  await b.context().clearCookies();
  await b.evaluate(() => window.dispatchEvent(new Event("focus")));
  await expect(b.getByText("1234567")).toHaveCount(0);
  await expect(b.getByRole("status").filter({ hasText: "振込先の表示を閉じました" })).toBeVisible();

  // 5) 受取人 A 自身の画面には振込先の表示ボタンを出さない
  await a.goto(toA);
  await expect(a.getByRole("heading", { name: "受け取りの確認" })).toBeVisible();
  await expect(a.getByRole("button", { name: "振込先を表示" })).toHaveCount(0);
});
