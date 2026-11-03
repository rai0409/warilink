import { expect, test } from "@playwright/test";
import { newUser } from "./helpers";

// PR B: 確定していない規約を正式な条件として見せない。削除が動いていない環境で自動削除を約束しない。

test("利用規約・プライバシーポリシーは準備中と表示し、下書きを正式な条件として出さない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  for (const [path, title] of [["/settings/terms", "利用規約"], ["/settings/privacy", "プライバシーポリシー"]]) {
    await a.goto(path);
    await expect(a.getByRole("heading", { name: title, level: 1 })).toBeVisible();
    await expect(a.getByRole("status")).toHaveText(`${title}は準備中です。`);
    await expect(a.getByText("OWNER_APPROVAL_REQUIRED")).toHaveCount(0);
    await expect(a.getByText(/^。/)).toHaveCount(0); // 以前の表示の崩れ(文頭の句点)
  }
});

test("保存期間は種類ごとに示し、削除の処理が動いていない環境では自動削除を約束しない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await a.goto("/settings");
  await a.getByRole("link", { name: /データの保存期間/ }).click();
  const list = a.getByRole("list", { name: "データの保存期間" });
  await expect(list.getByRole("listitem")).toHaveCount(7);
  await expect(list).toContainText("画像");
  await expect(list).toContainText("30日");
  await expect(list).toContainText("操作の記録");
  await expect(list).toContainText("1年");
  // E2E では削除ジョブを動かしていない
  await expect(a.getByRole("status").filter({ hasText: "確認できません" })).toBeVisible();
  await expect(a.getByText("自動で削除されます")).toHaveCount(0);
  await expect(a.getByText("最長30日残ります", { exact: false })).toBeVisible();
});

test("無料の手入力版: 止めている読み取り・広告を利用できるように見せず、送金しないことを示す", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  // サーバーの /plans はそのまま使い、読み取りと広告を止めた本番の既定値に差し替える(キーの形は変えない)
  await a.route("**/api/v1/plans", async (route) => {
    const res = await route.fetch();
    const body = await res.json();
    await route.fulfill({ response: res, json: { ...body, ocr_available: false, ads_available: false, billing_available: false } });
  });
  await a.goto("/pricing");
  const table = a.getByRole("table", { name: "料金プランの比較" });
  await expect(table).toBeVisible();
  await expect(table.getByRole("row", { name: /無料/ })).toContainText("停止中");
  await expect(table).not.toContainText(/\d+枚/);
  await expect(a.getByText(/レシート読み取りは月\d+枚まで/)).toHaveCount(0);
  await expect(a.getByText(/報酬広告の視聴/)).toHaveCount(0);
  await expect(a.getByText("登録なしの割り勘・精算はいつでも無料")).toBeVisible();
  await expect(a.getByText("レシート読み取りは停止中です(手入力で登録できます)")).toBeVisible();
  await expect(a.getByText(/いま提供しているのは、登録不要・手入力の割り勘と精算/)).toBeVisible();

  await a.goto("/");
  await expect(a.getByText("WariLinkは送金を実行しません", { exact: false })).toBeVisible();
  await expect(a.getByText("どちらも入金を保証するものではありません", { exact: false })).toBeVisible();
  await expect(a.getByText("元に戻せない場合があります", { exact: false })).toBeVisible();
});

test("読み取りを有効にしていても、広告を止めている間は広告で増える枚数を示さない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await a.route("**/api/v1/plans", async (route) => {
    const res = await route.fetch();
    await route.fulfill({ response: res, json: { ...(await res.json()), ocr_available: true, ads_available: false } });
  });
  await a.goto("/pricing");
  await expect(a.getByText(/報酬広告の視聴/)).toHaveCount(0);
  await expect(a.getByText("広告の視聴による読み取り枚数の追加は準備中です")).toBeVisible();
  await expect(a.getByRole("table", { name: "料金プランの比較" }).getByRole("row", { name: /無料/ })).not.toContainText("〜");
});
