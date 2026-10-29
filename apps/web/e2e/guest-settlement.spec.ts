import { expect, test } from "@playwright/test";
import { addExpense, createGroup, createInvite, groupHome, joinAs, newUser, openTab } from "./helpers";

// 実 API・実 DB を使う E2E(モックなし)。銀行・PayPay など外部サービスは一切呼ばない。

test("登録なしで開始→4人→支出→精算確定→コピー→送金申告→受取確認", async ({ browser, baseURL }, info) => {
  const clipboard = info.project.name === "desktop" ? { permissions: ["clipboard-read", "clipboard-write"] } : {};
  const a = await newUser(browser, { baseURL, ...clipboard });
  await createGroup(a, "週末キャンプ", "Bさん, Cさん, Dさん");
  const home = groupHome(a);

  // 招待URLで B が別ブラウザから参加(メール登録なし)
  const inviteUrl = await createInvite(a);
  const b = await newUser(browser, { baseURL, ...clipboard });
  await joinAs(b, inviteUrl, "Bさん", "週末キャンプ");

  // 権限: 招待URLの発行は作成者だけ。参加者 B にはメニューもボタンも出さず理由を示す
  await expect(b.getByRole("list", { name: "グループのメニュー" }).getByRole("link", { name: "招待リンク" })).toHaveCount(0);
  await b.goto(`${home}/invite`);
  await expect(b.getByText("招待リンクはグループを作成した人が発行できます。")).toBeVisible();
  await expect(b.getByRole("button", { name: "招待URLを作成" })).toHaveCount(0);

  // A が 10,001 円を立替、4人で均等割り(端数1円はサーバーが決定論的に配分)
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("バーベキュー");
  await a.getByLabel("金額(円)").fill("10,001");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  await expect(preview.getByRole("listitem")).toHaveCount(4);
  await expect(preview).toContainText("¥2,501");
  await expect(preview).toContainText("¥2,500");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("バーベキュー");
  await expect(a.getByText("受け取る予定")).toBeVisible();

  // メンバーの表示順は作成時の順で安定
  await openTab(a, "メンバー");
  await expect(a.getByRole("list", { name: "メンバー一覧" }).getByRole("listitem")).toHaveText([
    /^Aさん/, /^Bさん/, /^Cさん/, /^Dさん/,
  ]);

  // 3人から A への精算案(最少送金回数)
  await openTab(a, "精算");
  await expect(a.getByRole("heading", { name: "3回の送金で精算" })).toBeVisible();
  await expect(a.getByText("送金回数が最少になる組み合わせです。")).toBeVisible();
  await expect(a.getByRole("list", { name: "精算案" }).getByRole("listitem")).toHaveCount(3);
  await a.getByRole("button", { name: "精算案を確定" }).click();
  const confirmed = a.getByRole("list", { name: "確定した精算" });
  await expect(confirmed.getByRole("listitem")).toHaveCount(3);
  await expect(confirmed).toContainText("未送金");

  // B: 送金の記録 → 金額コピー → 送金申告(アプリは送金しない)
  await b.goto(`${home}/settle`);
  await b.getByRole("list", { name: "確定した精算" }).getByRole("link").filter({ hasText: "あなた" }).click();
  await expect(b.getByRole("heading", { name: "送金の記録", level: 1 })).toBeVisible();
  await expect(b.getByText("未送金").first()).toBeVisible();
  if (info.project.name === "desktop") {
    await b.getByRole("button", { name: "金額をコピー" }).click();
    await expect(b.getByText("コピーしました")).toBeVisible();
    // 端数の1円を誰が持つかはサーバーが決める(B は 2,500円 か 2,501円)
    expect(await b.evaluate(() => navigator.clipboard.readText())).toMatch(/^250[01]$/);
  }
  await b.getByRole("button", { name: "送金したと申告" }).click();
  await expect(b.getByRole("status")).toContainText("送金したと申告しました");
  await expect(b.getByText("送金の申告あり")).toBeVisible();
  // 申告だけでは「受け取り済み」と表示しない
  await expect(b.getByText("受け取り済み")).toHaveCount(0);
  await expect(b.getByText("WariLinkは送金を実行しません。")).toBeVisible();

  // A: 受け取りの確認。いったん「まだ受け取っていない」で未送金に戻す
  await a.goto(`${home}/settle`);
  const fromB = a.getByRole("list", { name: "確定した精算" }).getByRole("link").filter({ hasText: "Bさん" });
  await expect(fromB).toContainText("送金の申告あり");
  await fromB.click();
  await expect(a.getByRole("heading", { name: "受け取りの確認", level: 1 })).toBeVisible();
  await expect(a.getByText("入金を確認してから記録してください。")).toBeVisible();
  await a.getByRole("button", { name: "まだ受け取っていない" }).click();
  await expect(a.getByRole("status")).toContainText("まだ受け取っていないことを記録しました");
  await expect(a.getByText("未送金").first()).toBeVisible();
  await expect(a.getByRole("heading", { name: "ほかの受け取り" })).toBeVisible();

  // B がもう一度申告 → A が受け取りを記録
  await b.reload();
  await b.getByRole("button", { name: "送金したと申告" }).click();
  await expect(b.getByText("送金の申告あり")).toBeVisible();
  await a.reload();
  await a.getByRole("button", { name: "受け取った" }).click();
  await expect(a.getByRole("status")).toContainText("受け取りを記録しました");
  await expect(a.getByText("受け取り済み")).toBeVisible();

  // 送金の実行を示唆する文言・ボタンを出していない
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
