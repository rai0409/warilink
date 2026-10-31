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
