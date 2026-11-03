import { expect, test, type Page } from "@playwright/test";
import { createGroup, groupHome, itemButton, newUser, openTab, sumOf } from "./helpers";

// 商品別割り勘: 数量単位の割当・まとめて割当・外税の比例配分・総額不一致の明示解消・編集履歴

async function addItem(page: Page, name: string, qty: string, amount: string, first = false) {
  if (first) await itemButton(page, "商品1").click();
  else await page.getByRole("button", { name: "商品を追加" }).click();
  const panel = page.getByRole("dialog", { name: "商品" });
  await panel.getByLabel("商品名").fill(name);
  await panel.getByLabel("数量", { exact: true }).fill(qty);
  await panel.getByLabel("金額(円)").fill(amount);
  await panel.getByRole("button", { name: "完了" }).click();
}

/** 選んだ商品を割り当てる。members を渡すと均等割りでその人だけにする */
async function assign(page: Page, items: string[], members?: string[]) {
  for (const it of items) await page.getByLabel(`${it}を選択`).check();
  await page.getByRole("button", { name: `選択した${items.length}商品を割り当て` }).click();
  const panel = page.getByRole("dialog", { name: "割り当て" });
  if (members) {
    const list = panel.getByRole("list", { name: "負担する人" });
    for (const box of await list.getByRole("checkbox").all()) await box.uncheck();
    for (const m of members) await list.getByLabel(m).check();
  }
  return panel;
}

test("レシートを商品別に割り当て、不一致は明示的に解消してから登録できる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "居酒屋の会", "Bさん, Cさん");

  // 支出を追加 → 分け方「商品別」でレシート入力へ
  await openTab(a, "支出を追加");
  await a.getByRole("button", { name: "分け方" }).click();
  await a.getByLabel("商品別").click(); // 選ぶとレシート入力の画面へ移る
  await expect(a.getByRole("heading", { name: "商品別", level: 1 })).toBeVisible();
  await expect(a.getByText("レシートを撮影・画像を選ぶ")).toBeVisible(); // 手入力と読み取りのどちらも選べる

  await a.getByLabel("店名・内容").fill("居酒屋");
  await a.getByLabel("レシートの合計金額(円)").fill("3,700"); // 正しくは 3,685(わざと誤入力)

  // 生ビール 3杯 1,650円 を A 2杯・B 1杯
  await addItem(a, "生ビール", "3", "1650", true);
  let panel = await assign(a, ["生ビール"]);
  await panel.getByRole("button", { name: "数量", exact: true }).click();
  await panel.getByRole("button", { name: "Aさんの数量を増やす" }).click();
  await panel.getByRole("button", { name: "Aさんの数量を増やす" }).click();
  await panel.getByRole("button", { name: "Bさんの数量を増やす" }).click();
  await expect(panel.getByText("割当 3 / 3")).toBeVisible();
  await panel.getByRole("button", { name: "1商品に適用" }).click();
  await expect(itemButton(a, "生ビール")).toContainText("数量で配分");

  // 唐揚げ 800円・サラダ 600円 をまとめて全員で
  await addItem(a, "唐揚げ", "1", "800");
  await addItem(a, "サラダ", "1", "600");
  panel = await assign(a, ["唐揚げ", "サラダ"]);
  await panel.getByRole("button", { name: "2商品に適用" }).click();
  await expect(itemButton(a, "サラダ")).toContainText("Aさん、Bさん、Cさん");

  // 枝豆 300円 は未割当のまま → 登録不可
  await addItem(a, "枝豆", "1", "300");
  await expect(itemButton(a, "枝豆")).toContainText("未割当");
  await a.getByRole("button", { name: "内訳を確認" }).click();
  const summary = a.getByRole("dialog", { name: "内訳を確認" });
  await expect(summary.getByRole("button", { name: "登録する" })).toBeDisabled();
  await summary.getByRole("button", { name: "戻る" }).click();

  // 枝豆を B・C に
  panel = await assign(a, ["枝豆"], ["Bさん", "Cさん"]);
  await panel.getByRole("button", { name: "1商品に適用" }).click();
  await expect(itemButton(a, "枝豆")).toContainText("Bさん、Cさん");

  // 外税 10% 335円を負担額に比例して配分
  await a.getByRole("button", { name: /税・割引・サービス料/ }).click();
  const adjust = a.getByRole("dialog", { name: "税・割引・サービス料" });
  await adjust.getByLabel("税の金額(円)").fill("335");

  // 合計 3,685 ≠ 3,700 → 自動補正せず差額 15円 を提示、登録不可
  const mismatch = adjust.getByRole("region", { name: "合計金額の不一致" });
  await expect(mismatch).toContainText("差額 ¥15");
  await mismatch.getByRole("button", { name: "差額 ¥15 を調整行として追加" }).click();
  await expect(adjust.getByRole("region", { name: "合計金額の不一致" })).toHaveCount(0);
  await adjust.getByRole("button", { name: "適用する" }).click();

  await a.getByRole("button", { name: "内訳を確認" }).click();
  await expect(summary.getByText("明細の合計がレシートの合計と一致しました。")).toBeVisible();
  const shares = summary.getByRole("list", { name: "メンバー別の負担" });
  await expect(shares.getByRole("listitem")).toHaveCount(3);
  expect(await sumOf(shares)).toBe(3700);
  await summary.getByRole("button", { name: "登録する" }).click();

  const confirmed = a.getByRole("list", { name: "確定した負担額" });
  await expect(confirmed).toBeVisible();
  expect(await sumOf(confirmed)).toBe(3700);

  // 編集: ビールを A 1杯・B 2杯 に変更 → 履歴に残る
  await a.getByRole("button", { name: "編集する" }).click();
  panel = await assign(a, ["生ビール"]);
  await panel.getByRole("button", { name: "Aさんの数量を減らす" }).click();
  await panel.getByRole("button", { name: "Bさんの数量を増やす" }).click();
  await panel.getByRole("button", { name: "1商品に適用" }).click();
  await a.getByRole("button", { name: "内訳を確認" }).click();
  await summary.getByRole("button", { name: "変更を保存" }).click();
  const history = a.getByRole("list", { name: "変更履歴" });
  await expect(history.locator(":scope > li")).toHaveCount(2);
  await expect(history).toContainText("負担額を変更");
  expect(await sumOf(a.getByRole("list", { name: "確定した負担額" }))).toBe(3700);
});

test("同じ支出を2つの画面で同時に編集すると、後の保存は上書きせずに競合を知らせる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "競合テスト", "Bさん");
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("タクシー");
  await a.getByLabel("金額(円)").fill("3000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥1,500");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await a.getByRole("list", { name: "支出" }).getByRole("link", { name: /タクシー/ }).click();
  await expect(a).toHaveURL(/\/expenses\/[0-9a-f-]{36}$/);
  await expect(a.getByRole("button", { name: "編集する" })).toBeVisible();
  const detailUrl = a.url();

  // 同じ利用者の別タブで先に保存
  const a2 = await a.context().newPage();
  await a2.goto(detailUrl);
  await a.getByRole("button", { name: "編集する" }).click();
  await a2.getByRole("button", { name: "編集する" }).click();
  await a2.getByLabel("金額(円)").fill("3200");
  await expect(a2.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥1,600");
  await a2.getByRole("button", { name: "変更を保存" }).click();
  await expect(a2.getByRole("list", { name: "変更履歴" }).locator(":scope > li")).toHaveCount(2);

  await a.getByLabel("金額(円)").fill("4000");
  await expect(a.getByRole("list", { name: "負担額のプレビュー" })).toContainText("¥2,000");
  await a.getByRole("button", { name: "変更を保存" }).click();
  await expect(a.getByText("編集中に更新がありました")).toBeVisible();
  // 先に保存した内容(3,200円)は上書きされていない
  await a.getByRole("button", { name: "最新の内容を確認" }).click();
  await expect(a.getByText("¥3,200").first()).toBeVisible();
});

test("商品を削除すると、その商品を対象にした値引きからも外し、対象がなくなったら選び直してもらう", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "値引きの対象", "Bさん, Cさん");
  const home = groupHome(a);
  const g = await (await a.request.get(`/api/v1/groups/${home.split("/")[2]}`)).json();
  const id = (name: string) => g.members.find((m: { display_name: string }) => m.display_name === name).id;
  const item = (iid: string, name: string, line: number, who: string) => ({
    id: iid, name, line_minor: line, quantity: 1,
    allocation: { mode: "equal", shares: [{ member_id: id(who), value: 1 }] },
  });
  // 読み取り結果から作った支出と同じ形: 値引きは餃子とサラダだけが対象
  const r = await a.request.post(`/api/v1/groups/${g.public_id}/expenses`, {
    headers: { "X-WariLink-CSRF": "1", "Idempotency-Key": crypto.randomUUID() },
    data: {
      title: "中華", amount_minor: 1500, payers: [{ member_id: id("Aさん"), amount_minor: 1500 }],
      split: { mode: "itemized", receipt: { receipt_total_minor: 1500,
        items: [item("ramen", "ラーメン", 900, "Aさん"), item("gyoza", "餃子", 400, "Bさん"), item("salad", "サラダ", 300, "Cさん")],
        adjustments: [{ id: "d", kind: "discount", label: "セット割", amount_minor: -100,
          rule: { type: "proportional", scope_item_ids: ["gyoza", "salad"] } }] } },
    },
  });
  expect(r.status()).toBe(201);
  await a.goto(`${home}/expenses/${(await r.json()).id}`);
  await a.getByRole("button", { name: "編集する" }).click();
  await expect(itemButton(a, "餃子")).toBeVisible();

  // 1) 餃子を削除 → 値引きの対象はサラダだけになる(ラーメンには広げない)
  const preview = a.waitForRequest((q) => q.url().includes("/expenses/preview")
    && q.postDataJSON()?.split?.receipt?.items?.length === 2);
  await itemButton(a, "餃子").click();
  await a.getByRole("dialog", { name: "商品" }).getByRole("button", { name: "この商品を削除" }).click();
  const sent = (await preview).postDataJSON().split.receipt.adjustments;
  expect(sent.find((x: { id: string }) => x.id === "d").rule.scope_item_ids).toEqual(["salad"]);
  await a.getByRole("button", { name: "税・割引・サービス料" }).click();
  const adjust = a.getByRole("dialog", { name: "税・割引・サービス料" });
  const scope = adjust.getByRole("combobox", { name: "セット割の対象" });
  await expect(scope.locator("option:checked")).toHaveText("サラダのみ");
  await adjust.getByRole("button", { name: "戻る" }).click();

  // 2) サラダも削除 → 対象がなくなった値引きは全商品へ広げず、選び直すまで保存できない
  await itemButton(a, "サラダ").click();
  await a.getByRole("dialog", { name: "商品" }).getByRole("button", { name: "この商品を削除" }).click();
  await a.getByRole("button", { name: "税・割引・サービス料" }).click();
  await expect(scope.locator("option:checked")).toHaveText("削除した商品(選び直してください)");
  await expect(adjust.getByText("配分対象の商品がありません")).toBeVisible();
});
