import { expect, test } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// PR D: 画面の操作の頑健さ。多重タップ・戻る/進む・通信の失敗・全角の金額で、支出が二重に登録されず、金額が崩れない。

test("登録ボタンを連打しても支出は 1 件だけ", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "連打", "Bさん");
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("ランチ");
  await a.getByLabel("金額(円)").fill("1,200");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥600");
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeEnabled();
  // 同じボタンを続けて 3 回押す(画面が切り替わる前の多重タップ)
  await a.evaluate(() => {
    const b = [...document.querySelectorAll("button")].find((x) => x.textContent === "この内容で登録");
    for (let i = 0; i < 3; i++) b?.click();
  });
  await expect(a.getByRole("list", { name: "支出" })).toContainText("ランチ");
  await a.reload();
  await expect(a.getByRole("list", { name: "支出" }).getByRole("listitem")).toHaveCount(1);
});

test("全角の数字・カンマの金額は整数円として扱い、不正な値では登録できない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "全角", "Bさん");
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("宿");
  await a.getByLabel("金額(円)").fill("１２，３４５");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  await expect(preview).toContainText("¥6,173");
  await expect(preview).toContainText("¥6,172");
  await a.getByLabel("金額(円)").fill("12.5");
  await expect(a.getByText("金額は整数の円で入力してください")).toBeVisible();
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeDisabled();
});

test("通信が切れたときは日本語で知らせ、戻ったら 1 件だけ登録できる。戻る・進むで二重登録しない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "通信", "Bさん");
  const home = a.url();
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("タクシー");
  await a.getByLabel("金額(円)").fill("3000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥1,500");
  await a.context().setOffline(true);
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("alert").filter({ hasText: "通信に失敗しました" })).toBeVisible();
  await expect(a.getByRole("alert").filter({ hasText: /TypeError|fetch|Failed/ })).toHaveCount(0); // 技術的な文字列を出さない
  await a.context().setOffline(false);
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("タクシー");
  // 戻る(登録画面へ)→ 進む(ホームへ): 再送しない
  await a.goBack();
  await a.goForward();
  await expect(a).toHaveURL(home);
  await a.reload();
  await expect(a.getByRole("list", { name: "支出" }).getByRole("listitem")).toHaveCount(1);
});
