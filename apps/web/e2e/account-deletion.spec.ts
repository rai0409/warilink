import { expect, test } from "@playwright/test";
import { createGroup, createInvite, groupHome, joinAs, newUser, openTab } from "./helpers";

// STEP4: グループからの離脱と退会。どちらも確認してから行い、他のメンバーの記録は残る。

test("参加者はグループから抜けられ、退会すると戻れない(記録は名前だけ残る)", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "離脱テスト", "Bさん, Cさん");
  const home = groupHome(a);
  const inviteUrl = await createInvite(a);

  // B が参加して抜ける(キャンセルすれば何もしない)
  const b = await newUser(browser, { baseURL });
  await joinAs(b, inviteUrl, "Bさん", "離脱テスト");
  await openTab(b, "グループ設定");
  await b.getByRole("button", { name: "グループから抜ける" }).click();
  await b.getByRole("dialog").getByRole("button", { name: "キャンセル" }).click();
  await expect(b.getByRole("dialog")).toHaveCount(0);
  await b.getByRole("button", { name: "グループから抜ける" }).click();
  await b.getByRole("dialog").getByRole("button", { name: "抜ける", exact: true }).click();
  await expect(b).toHaveURL(/\/$/);
  await b.goto(home);
  await expect(b.getByRole("alert").filter({ hasText: /\S/ })).toBeVisible(); // もう見られない

  // 作成者には、引き継ぎと終了の操作を出す(抜けると自動で引き継ぐ)
  await openTab(a, "グループ設定");
  await expect(a.getByRole("heading", { name: "管理" })).toBeVisible();
  await expect(a.getByRole("link", { name: "作成者を引き継ぐ" })).toBeVisible();

  // C が参加して退会する
  const c = await newUser(browser, { baseURL });
  await joinAs(c, inviteUrl, "Cさん", "離脱テスト");
  await c.goto("/settings");
  await expect(c.getByText("ゲストとして利用中")).toBeVisible();
  await c.getByRole("button", { name: "退会する" }).click();
  const dialog = c.getByRole("dialog", { name: "退会しますか?" });
  await expect(dialog).toContainText("元に戻せません");
  await dialog.getByRole("button", { name: "退会する" }).click();
  await expect(c).toHaveURL(/\/\?deleted=1$/);
  await c.goto(home);
  await expect(c.getByRole("alert").filter({ hasText: /\S/ })).toBeVisible();

  // A から見ると B・C は名前だけのメンバーとして残る
  await openTab(a, "メンバー");
  const members = a.getByRole("list", { name: "メンバー一覧" });
  await expect(members.getByRole("listitem")).toHaveCount(3);
  await expect(members.getByText("名前のみ")).toHaveCount(2);

  // 作成者がグループを終了すると、閲覧だけになる
  await openTab(a, "グループ設定");
  await a.getByRole("button", { name: /グループを終了する/ }).click();
  const close = a.getByRole("dialog", { name: "グループを終了しますか?" });
  await expect(close).toContainText("閲覧だけ");
  await close.getByRole("button", { name: "終了する" }).click();
  await expect(a.getByText("このグループは終了しています。閲覧だけできます。")).toBeVisible();
  await expect(a.getByRole("button", { name: /グループを終了する/ })).toHaveCount(0);
  await a.goto(home);
  await expect(a.getByRole("link", { name: "支出を追加" })).toHaveCount(0);
});
