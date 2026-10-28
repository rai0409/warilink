import { expect, test } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// STEP4: グループからの離脱と退会。どちらも確認してから行い、他のメンバーの記録は残る。

test("参加者はグループから抜けられ、退会すると戻れない(記録は名前だけ残る)", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "離脱テスト", "Bさん, Cさん");
  await a.getByRole("button", { name: "招待URLを作成" }).click();
  const inviteUrl = await a.getByLabel(/招待URL/).inputValue();

  // B が参加して抜ける(取り消せば何もしない)
  const b = await newUser(browser, { baseURL });
  await b.goto(inviteUrl);
  await b.getByRole("button", { name: "Bさん" }).click();
  await expect(b.getByRole("heading", { name: "離脱テスト" })).toBeVisible();
  const groupUrl = b.url();
  await openTab(b, "設定");
  b.once("dialog", (d) => d.dismiss());
  await b.getByRole("button", { name: "このグループから抜ける" }).click();
  await expect(b.getByRole("button", { name: "このグループから抜ける" })).toBeVisible();
  b.once("dialog", (d) => d.accept());
  await b.getByRole("button", { name: "このグループから抜ける" }).click();
  await expect(b).toHaveURL(/\/$/);
  await b.goto(groupUrl);
  await expect(b.getByRole("alert")).toBeVisible(); // もう見られない

  // 作成者には、引き継ぎと終了の操作を出す(抜けると自動で引き継ぐ)
  await openTab(a, "設定");
  await expect(a.getByRole("heading", { name: "作成者の引き継ぎ・グループの終了" })).toBeVisible();

  // C が参加して退会する
  const c = await newUser(browser, { baseURL });
  await c.goto(inviteUrl);
  await c.getByRole("button", { name: "Cさん" }).click();
  await expect(c.getByRole("heading", { name: "離脱テスト" })).toBeVisible();
  await c.goto("/account");
  c.once("dialog", (d) => {
    expect(d.message()).toContain("元に戻せません");
    d.accept();
  });
  await c.getByRole("button", { name: "アカウントを削除する" }).click();
  await expect(c).toHaveURL(/\/\?deleted=1$/);
  await c.goto(groupUrl);
  await expect(c.getByRole("alert")).toBeVisible();

  // A から見ると B・C は名前だけのメンバーとして残る
  await a.reload();
  await expect(a.getByText("参加済み")).toHaveCount(1); // A 自身だけ
  await expect(a.getByText("未参加")).toHaveCount(2);

  // 作成者がグループを終了すると、閲覧だけになる
  a.once("dialog", (d) => {
    expect(d.message()).toContain("閲覧だけ");
    d.accept();
  });
  await a.getByRole("button", { name: "グループを終了する" }).click();
  await expect(a.getByRole("status").filter({ hasText: "グループを終了しました" })).toBeVisible();
  await expect(a.getByText("このグループは終了しています。閲覧だけできます。")).toBeVisible();
  await expect(a.getByRole("button", { name: "グループを終了する" })).toHaveCount(0);
});
