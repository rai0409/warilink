import { expect, test } from "@playwright/test";
import { addExpense, createGroup, newUser, openTab } from "./helpers";

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

test("古いプレビューでは登録できず、同じ入力を再試行して登録できる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "プレビュー再試行", "Bさん");
  await openTab(a, "支出を追加");
  let fail = true;
  let posts = 0;
  a.on("request", (request) => {
    if (request.method() === "POST" && /\/api\/v1\/groups\/[^/]+\/expenses$/.test(new URL(request.url()).pathname)) posts++;
  });
  await a.route("**/api/v1/groups/*/expenses/preview", async (route) => {
    if (route.request().method() === "POST" && route.request().postDataJSON().amount_minor === 2000 && fail) {
      await route.abort("failed");
    } else await route.continue();
  });
  await a.getByLabel("内容").fill("昼食");
  await a.getByLabel("金額(円)").fill("1000");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  const save = a.getByRole("button", { name: "この内容で登録" });
  await expect(preview).toContainText("¥500");
  await a.getByLabel("金額(円)").fill("2000");
  await expect(a.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(preview).toHaveCount(0);
  await expect(save).toBeDisabled();
  expect(posts).toBe(0);
  fail = false;
  await a.getByRole("button", { name: "再試行" }).click();
  await expect(a.getByLabel("金額(円)")).toHaveValue("2000");
  await expect(preview).toContainText("¥1,000");
  await expect(save).toBeEnabled();
  await save.click();
  const expenses = a.getByRole("list", { name: "支出" });
  await expect(expenses.getByRole("listitem")).toHaveCount(1);
  await expect(expenses).toContainText("¥2,000");
  expect(posts).toBe(1);
});

test("逆順に完了した古いプレビューは新しい結果を上書きしない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "応答順序", "Bさん");
  await openTab(a, "支出を追加");
  let release!: () => void;
  let received!: () => void;
  let finished!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const requested = new Promise<void>((resolve) => { received = resolve; });
  const completed = new Promise<void>((resolve) => { finished = resolve; });
  await a.route("**/api/v1/groups/*/expenses/preview", async (route) => {
    if (route.request().method() === "POST" && route.request().postDataJSON().amount_minor === 1000) {
      const response = await route.fetch();
      received();
      await held;
      await route.fulfill({ response });
      finished();
    } else await route.continue();
  });
  await a.getByLabel("内容").fill("順序");
  await a.getByLabel("金額(円)").fill("1000");
  await requested;
  await a.getByLabel("金額(円)").fill("2000");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  await expect(preview).toContainText("¥1,000");
  release();
  await completed;
  await expect(preview).toContainText("¥1,000");
  await expect(preview).not.toContainText("¥500");
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeEnabled();
});

test("編集時もプレビュー失敗後に再試行して保存できる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "編集再試行", "Bさん");
  await addExpense(a, "編集対象", "1000");
  await a.getByRole("list", { name: "支出" }).getByRole("link", { name: /編集対象/ }).click();
  await a.getByRole("button", { name: "編集する" }).click();
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥500");
  let fail = true;
  let puts = 0;
  let ifMatch: string | undefined;
  a.on("request", (request) => {
    if (request.method() === "PUT" && /\/api\/v1\/groups\/[^/]+\/expenses\/[^/]+$/.test(new URL(request.url()).pathname)) {
      puts++;
      ifMatch = request.headers()["if-match"];
    }
  });
  await a.route("**/api/v1/groups/*/expenses/preview", async (route) => {
    if (route.request().method() === "POST" && fail) await route.abort("failed");
    else await route.continue();
  });
  await a.getByLabel("金額(円)").fill("2000");
  await expect(a.getByRole("button", { name: "再試行" })).toBeVisible();
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toHaveCount(0);
  await expect(a.getByRole("button", { name: "変更を保存" })).toBeDisabled();
  expect(puts).toBe(0);
  fail = false;
  await a.getByRole("button", { name: "再試行" }).click();
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥1,000");
  await a.getByRole("button", { name: "変更を保存" }).click();
  await expect(a.getByRole("list", { name: "変更履歴" }).locator(":scope > li")).toHaveCount(2);
  await expect(a.getByText("¥2,000").first()).toBeVisible();
  expect(puts).toBe(1);
  expect(ifMatch).toMatch(/^\d+$/);
});

test("入力を元に戻しても新しいプレビューの成功まで保存できない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "入力を戻す", "Bさん");
  await openTab(a, "支出を追加");
  let countA = 0;
  let release!: () => void;
  let received!: () => void;
  const held = new Promise<void>((resolve) => { release = resolve; });
  const requested = new Promise<void>((resolve) => { received = resolve; });
  await a.route("**/api/v1/groups/*/expenses/preview", async (route) => {
    if (route.request().method() === "POST" && route.request().postDataJSON().amount_minor === 1000 && ++countA === 2) {
      received();
      await held;
    }
    await route.continue();
  });
  await a.getByLabel("内容").fill("往復");
  await a.getByLabel("金額(円)").fill("1000");
  const preview = a.getByRole("list", { name: "負担額のプレビュー" });
  const save = a.getByRole("button", { name: "この内容で登録" });
  await expect(preview).toContainText("¥500");
  await a.getByLabel("金額(円)").fill("2000");
  await expect(preview).toHaveCount(0);
  await a.getByLabel("金額(円)").fill("1000");
  await requested;
  await expect(preview).toHaveCount(0);
  await expect(save).toBeDisabled();
  release();
  await expect(preview).toContainText("¥500");
  await expect(save).toBeEnabled();
});
