import { expect, test } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// 実 API・実 DB を使う E2E(モックなし)。銀行・PayPay など外部サービスは一切呼ばない。

test("登録なしで開始→4人→支出→精算確定→コピー→送金申告→受取確認", async ({ browser, baseURL }, info) => {
  const clipboard = info.project.name === "desktop" ? { permissions: ["clipboard-read", "clipboard-write"] } : {};
  const a = await newUser(browser, { baseURL, ...clipboard });
  await createGroup(a, "週末キャンプ", "Bさん, Cさん, Dさん");

  // 招待URLで B が別ブラウザから参加(メール登録なし)
  await a.getByRole("button", { name: "招待URLを作成" }).click();
  const inviteUrl = await a.getByLabel(/招待URL/).inputValue();
  const b = await newUser(browser, { baseURL, ...clipboard });
  await b.goto(inviteUrl);
  await b.getByRole("button", { name: "Bさん" }).click();
  await expect(b.getByRole("heading", { name: "週末キャンプ" })).toBeVisible();

  // A が 10,001 円を立替、4人で均等割り(端数1円はサーバーが決定論的に配分)
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("バーベキュー");
  await a.getByLabel("金額(円)").fill("10,001");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  await expect(preview.getByRole("listitem")).toHaveCount(4);
  await expect(preview).toContainText("2,501円");
  await expect(preview).toContainText("2,500円");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("バーベキュー");

  // メンバーの表示順は作成時の順で安定
  await expect(a.getByRole("region", { name: "メンバーと残高" }).getByRole("listitem")).toHaveText([
    /^Aさん/, /^Bさん/, /^Cさん/, /^Dさん/,
  ]);

  // 3人から A への精算案(最小送金回数)
  await openTab(a, "精算");
  await expect(a.getByText("最小送金回数")).toBeVisible();
  await expect(a.getByRole("list", { name: "精算案" }).getByRole("listitem")).toHaveCount(3);
  await a.getByRole("button", { name: "この精算案で確定" }).click();
  const confirmed = a.getByRole("list", { name: "確定した精算" });
  await expect(confirmed.getByRole("listitem").filter({ hasText: "→" })).toHaveCount(3);

  // B: 金額コピー → 送金申告(アプリは送金しない)
  await b.reload();
  await openTab(b, "精算");
  const bRow = b.getByRole("list", { name: "確定した精算" }).locator("li.transfer").filter({ hasText: "Bさん → Aさん" });
  if (info.project.name === "desktop") {
    await bRow.getByRole("button", { name: "金額をコピー" }).click();
    await expect(bRow.getByRole("button", { name: "コピーしました" })).toBeVisible();
  }
  await bRow.getByRole("button", { name: "送金したと申告" }).click();
  await expect(bRow).toContainText("送金申告済み(未確認)");

  // A: 受取人として手動確認
  await a.reload();
  const aRow = a.getByRole("list", { name: "確定した精算" }).locator("li.transfer").filter({ hasText: "Bさん → Aさん" });
  await aRow.getByRole("button", { name: "受け取りを確認" }).click();
  await expect(aRow).toContainText("受取人が確認済み");

  // 送金の実行を示唆する文言を出していない
  await expect(a.getByText("このアプリは送金を実行しません").first()).toBeVisible();
  await expect(a.getByRole("button", { name: /^送金する$/ })).toHaveCount(0);
});

test("グループ外のユーザーはURLを知っていても閲覧できない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "秘密の会", "");
  const url = a.url();

  const outsider = await newUser(browser, { baseURL });
  await outsider.goto("/");
  await outsider.request.post("/api/v1/identity/guest", { headers: { "X-WariLink-CSRF": "1" } });
  for (const path of ["", "/settle", "/receipt", "/settings"]) {
    await outsider.goto(url + path);
    await expect(outsider.getByText("見つかりませんでした")).toBeVisible();
    await expect(outsider.getByRole("heading", { name: "秘密の会" })).toHaveCount(0);
  }
});
