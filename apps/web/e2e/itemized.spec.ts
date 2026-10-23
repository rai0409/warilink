import { expect, test, type Locator, type Page } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// 商品別割り勘: 数量単位の割当・全員・前と同じ・一括割当・外税の比例配分・総額不一致の明示解消・編集履歴

async function fillItem(item: Locator, name: string, qty: string, amount: string) {
  await item.getByLabel("商品名").fill(name);
  await item.getByLabel("数量", { exact: true }).fill(qty);
  await item.getByLabel("金額(円)").fill(amount);
}

async function sumOf(list: Locator): Promise<number> {
  const texts = await list.locator(".amount").allTextContents();
  return texts.reduce((s, t) => s + Number(t.replace(/[^\d−-]/g, "").replace("−", "-")), 0);
}

const item = (page: Page, n: number) => page.getByRole("group", { name: `商品${n}`, exact: true });

test("レシートを商品別に割り当て、不一致は明示的に解消してから登録できる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "居酒屋の会", "Bさん, Cさん");
  await openTab(a, "レシート");
  await expect(a.getByLabel("レシートを撮影・画像を選ぶ")).toBeVisible(); // 手入力と読み取りのどちらも選べる

  await a.getByLabel("店名・内容").fill("居酒屋");
  await a.getByLabel("レシートの合計金額(円)").fill("3,700"); // 正しくは 3,685(わざと誤入力)

  // 1: 生ビール 3杯 1,650円 を A 2杯・B 1杯
  await fillItem(item(a, 1), "生ビール", "3", "1650");
  await item(a, 1).getByLabel("分け方").selectOption("units");
  await item(a, 1).getByRole("button", { name: "Aさんの数量を増やす" }).click();
  await item(a, 1).getByRole("button", { name: "Aさんの数量を増やす" }).click();
  await item(a, 1).getByRole("button", { name: "Bさんの数量を増やす" }).click();
  await expect(item(a, 1).getByLabel("この商品の負担")).toHaveText("Aさん 1,100円 · Bさん 550円");

  // 2: 唐揚げ 800円 を全員
  await a.getByRole("button", { name: "商品を追加" }).click();
  await fillItem(item(a, 2), "唐揚げ", "1", "800");
  await item(a, 2).getByRole("button", { name: "全員" }).click();

  // 3: サラダ 600円 は「前の商品と同じ」(全員)
  await a.getByRole("button", { name: "商品を追加" }).click();
  await fillItem(item(a, 3), "サラダ", "1", "600");
  await item(a, 3).getByRole("button", { name: "前の商品と同じ" }).click();
  await expect(item(a, 3).getByRole("button", { name: "Cさん" })).toHaveAttribute("aria-pressed", "true");

  // 4: 枝豆 300円 は未割当のまま → 登録不可
  await a.getByRole("button", { name: "商品を追加" }).click();
  await fillItem(item(a, 4), "枝豆", "1", "300");
  await expect(item(a, 4).getByText("未割当")).toBeVisible();
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeDisabled();

  // 3 と 4 を一括で B・C に割り当て
  await item(a, 3).getByLabel("一括割当の対象").check();
  await item(a, 4).getByLabel("一括割当の対象").check();
  const bulk = a.getByRole("region", { name: "一括割当" });
  await bulk.getByRole("button", { name: "Bさん" }).click();
  await bulk.getByRole("button", { name: "Cさん" }).click();
  await bulk.getByRole("button", { name: "選択した商品に割り当てる" }).click();
  await expect(item(a, 4).getByText("未割当")).toHaveCount(0);
  await expect(item(a, 3).getByLabel("この商品の負担")).toHaveText("Bさん 300円 · Cさん 300円");

  // 外税 10% 335円を負担額に比例して配分
  await a.getByRole("button", { name: "税・割引などを追加" }).click();
  const tax = a.getByRole("group", { name: "調整1" });
  await tax.getByLabel("金額(円)").fill("335");

  // 合計 3,685 ≠ 3,700 → 自動補正せず差額 15円 を提示、登録不可
  const mismatch = a.getByRole("region", { name: "合計金額の不一致" });
  await expect(mismatch).toContainText("差額 15円");
  await expect(a.getByRole("button", { name: "この内容で登録" })).toBeDisabled();

  // ユーザーが明示的に差額調整行を追加して解消
  await mismatch.getByRole("button", { name: "差額 15円 を調整行として追加" }).click();
  await expect(a.getByText("明細の合計がレシートの合計 3,700円 と一致しました")).toBeVisible();
  const shares = a.getByRole("list", { name: "メンバー別の負担" });
  await expect(shares.getByRole("listitem")).toHaveCount(3);
  expect(await sumOf(shares)).toBe(3700);

  await a.getByRole("button", { name: "この内容で登録" }).click();
  const confirmed = a.getByRole("list", { name: "確定した負担額" });
  await expect(confirmed).toBeVisible();
  expect(await sumOf(confirmed)).toBe(3700);

  // 編集: ビールを A 1杯・B 2杯 に変更 → 版2、履歴に残る
  await a.getByRole("button", { name: "編集" }).click();
  await item(a, 1).getByRole("button", { name: "Aさんの数量を減らす" }).click();
  await item(a, 1).getByRole("button", { name: "Bさんの数量を増やす" }).click();
  await expect(item(a, 1).getByLabel("この商品の負担")).toHaveText("Aさん 550円 · Bさん 1,100円");
  await a.getByRole("button", { name: "変更を保存" }).click();
  await expect(a.getByText(/版 2/)).toBeVisible();
  await expect(a.getByRole("list", { name: "変更履歴" }).getByRole("listitem")).toHaveCount(2);
  expect(await sumOf(a.getByRole("list", { name: "確定した負担額" }))).toBe(3700);
});

test("同じ支出を2つの画面で同時に編集すると、後の保存は上書きせずに競合を知らせる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "競合テスト", "Bさん");
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("タクシー");
  await a.getByLabel("金額(円)").fill("3000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("1,500円");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await a.getByRole("link", { name: "タクシー" }).click();
  await expect(a).toHaveURL(/\/expenses\/[0-9a-f-]{36}$/);
  await expect(a.getByRole("button", { name: "編集" })).toBeVisible();
  const detailUrl = a.url();

  // 同じ利用者の別タブで先に保存
  const a2 = await a.context().newPage();
  await a2.goto(detailUrl);
  await a.getByRole("button", { name: "編集" }).click();
  await a2.getByRole("button", { name: "編集" }).click();
  await a2.getByLabel("金額(円)").fill("3200");
  await expect(a2.getByRole("list", { name: "負担額のプレビュー" })).toContainText("1,600円");
  await a2.getByRole("button", { name: "変更を保存" }).click();
  await expect(a2.getByText(/版 2/)).toBeVisible();

  await a.getByLabel("金額(円)").fill("4000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("2,000円");
  await a.getByRole("button", { name: "変更を保存" }).click();
  await expect(a.getByText(/他の人が先にこの支出を更新しました/)).toBeVisible();
  // 先に保存した内容(3,200円)は上書きされていない
  await a.reload();
  await expect(a.getByText("3,200円").first()).toBeVisible();
});
