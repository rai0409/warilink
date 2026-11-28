import { expect, test } from "@playwright/test";
import { createGroup, createInvite, newUser } from "./helpers";

test("招待 fragment を消し固定 API で参加する", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "秘密の招待", "Bさん");
  const invite = await createInvite(owner);
  const token = new URL(invite).hash.slice("#token=".length);
  expect(token.length).toBeGreaterThan(0);
  const guest = await newUser(browser);
  const paths: string[] = [];
  guest.on("request", (request) => paths.push(new URL(request.url()).pathname + new URL(request.url()).search));
  await guest.goto(invite);
  await guest.getByLabel("Bさん", { exact: true }).check();
  expect(new URL(guest.url()).hash).toBe("");
  expect(paths.every((path) => !path.includes(token))).toBeTruthy();
  expect(await guest.evaluate((t) => JSON.stringify({ ...localStorage, ...sessionStorage }).includes(t), token)).toBeFalsy();
  await guest.getByRole("button", { name: "Bさんとして参加" }).click();
  await expect(guest.getByRole("heading", { name: "秘密の招待", level: 1 })).toBeVisible();
  expect(paths.every((path) => !path.includes(token))).toBeTruthy();
});

test("コードと旧 URL と新 URL の貼り付け、再試行、再読込", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "貼り付け招待", "Bさん");
  const invite = await createInvite(owner);
  const parsed = new URL(invite);
  const token = new URLSearchParams(parsed.hash.slice(1)).get("token")!;
  for (const value of [token, `${parsed.origin}/join/${token}`, invite]) {
    const guest = await newUser(browser);
    await guest.goto("/join");
    await guest.getByLabel("招待リンク").fill(value);
    await guest.getByRole("button", { name: "次へ" }).click();
    await expect(guest.getByLabel("Bさん", { exact: true })).toBeVisible();
    expect(new URL(guest.url()).hash).toBe("");
    await guest.reload();
    await expect(guest.getByLabel("招待リンク")).toBeVisible();
    await guest.context().close();
  }
  const guest = await newUser(browser);
  await guest.route("**/api/v1/invites/preview", (route) => route.abort(), { times: 1 });
  await guest.goto(invite);
  await guest.getByRole("button", { name: "再試行" }).click();
  await expect(guest.getByLabel("Bさん", { exact: true })).toBeVisible();
  await guest.getByLabel("新しい名前", { exact: true }).check();
  await guest.getByRole("textbox", { name: "新しい名前" }).fill("新しい人");
  await guest.getByRole("button", { name: "新しい人として参加" }).click();
  await expect(guest.getByRole("heading", { name: "貼り付け招待", level: 1 })).toBeVisible();
});

test("作成者の保存済み旧 URL を安全な形式へ移行", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "保存移行", "Bさん");
  const invite = await createInvite(owner);
  const parsed = new URL(invite);
  const token = new URLSearchParams(parsed.hash.slice(1)).get("token")!;
  const groupId = new URL(owner.url()).pathname.split("/")[2];
  await owner.evaluate(({ id, url }) => localStorage.setItem(`warilink.invite.${id}`, url),
    { id: groupId, url: `${parsed.origin}/join/${token}` });
  await owner.goto(`/g/${groupId}/invite`);
  await expect(owner.getByLabel("招待URL")).toHaveValue(invite);
  expect(await owner.evaluate((id) => localStorage.getItem(`warilink.invite.${id}`), groupId)).toBe(invite);
});

test("同一画面で新しい招待に切り替え、古い preview を破棄", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "招待A", "Bさん");
  const inviteA = await createInvite(owner);
  await createGroup(owner, "招待B", "Cさん");
  const inviteB = await createInvite(owner);
  const guest = await newUser(browser);
  const tokenA = new URLSearchParams(new URL(inviteA).hash.slice(1)).get("token")!;
  const tokenB = new URLSearchParams(new URL(inviteB).hash.slice(1)).get("token")!;
  const paths: string[] = [];
  guest.on("request", (request) => paths.push(new URL(request.url()).pathname + new URL(request.url()).search));
  await guest.goto("/join");
  await guest.getByRole("button", { name: "次へ" }).click();
  await expect(guest.getByText("招待リンクの形式が正しくありません。届いたリンクをそのまま貼り付けてください")).toBeVisible();
  await guest.evaluate((hash) => { location.hash = hash; }, new URL(inviteA).hash);
  await expect(guest.getByText("招待A", { exact: true })).toBeVisible();
  await guest.getByLabel("Bさん", { exact: true }).check();
  await guest.evaluate((hash) => { location.hash = hash; }, new URL(inviteB).hash);
  await expect(guest.getByText("招待B", { exact: true })).toBeVisible();
  await expect(guest.getByLabel("Bさん", { exact: true })).toHaveCount(0);
  expect(new URL(guest.url()).hash).toBe("");
  await guest.getByLabel("Cさん", { exact: true }).check();
  await guest.getByRole("button", { name: "Cさんとして参加" }).click();
  await expect(guest.getByRole("heading", { name: "招待B", level: 1 })).toBeVisible();
  expect(paths.every((p) => !p.includes(tokenA) && !p.includes(tokenB))).toBeTruthy();

  const delayed = await newUser(browser);
  let release!: () => void;
  let arrived!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const seen = new Promise<void>((resolve) => { arrived = resolve; });
  await delayed.route("**/api/v1/invites/preview", async (route) => {
    if (route.request().postDataJSON().token === tokenA) {
      const response = await route.fetch();
      arrived();
      await waiting;
      await route.fulfill({ response });
    } else await route.continue();
  });
  await delayed.goto(inviteA);
  await seen;
  await delayed.evaluate((hash) => { location.hash = hash; }, new URL(inviteB).hash);
  await expect(delayed.getByText("招待B", { exact: true })).toBeVisible();
  const oldResponse = delayed.waitForResponse((response) => response.url().endsWith("/invites/preview")
    && response.request().postDataJSON().token === tokenA);
  release();
  await oldResponse;
  await delayed.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  await expect(delayed.getByText("招待A", { exact: true })).toHaveCount(0);
  await expect(delayed.getByText("招待B", { exact: true })).toBeVisible();
});

test("参加中に招待を切り替えても別 token で誤参加しない", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "参加A", "Bさん");
  const inviteA = await createInvite(owner);
  await createGroup(owner, "参加B", "Cさん");
  const inviteB = await createInvite(owner);
  const guest = await newUser(browser);
  let release!: () => void;
  let arrived!: () => void;
  const waiting = new Promise<void>((resolve) => { release = resolve; });
  const seen = new Promise<void>((resolve) => { arrived = resolve; });
  const tokenA = new URLSearchParams(new URL(inviteA).hash.slice(1)).get("token")!;
  await guest.route("**/api/v1/invites/join", async (route) => {
    expect(route.request().postDataJSON().token).toBe(tokenA);
    const response = await route.fetch();
    arrived();
    await waiting;
    await route.fulfill({ response });
  }, { times: 1 });
  await guest.goto(inviteA);
  await guest.getByLabel("Bさん", { exact: true }).check();
  await guest.getByRole("button", { name: "Bさんとして参加" }).click();
  await seen;
  await guest.evaluate((hash) => { location.hash = hash; }, new URL(inviteB).hash);
  await expect(guest.getByText("参加B", { exact: true })).toBeVisible();
  await guest.getByLabel("Cさん", { exact: true }).check();
  await expect(guest.getByRole("button", { name: "Cさんとして参加" })).toBeDisabled();
  release();
  await expect(guest.getByRole("button", { name: "Cさんとして参加" })).toBeEnabled();
  await guest.getByRole("button", { name: "Cさんとして参加" }).click();
  await expect(guest.getByRole("heading", { name: "参加B", level: 1 })).toBeVisible();
});

test("切替直後の旧画面の選択を新しい招待に送らない", async ({ browser }) => {
  const owner = await newUser(browser);
  await createGroup(owner, "世代A", "Bさん");
  const inviteA = await createInvite(owner);
  await createGroup(owner, "世代B", "Cさん");
  const inviteB = await createInvite(owner);
  const tokenB = new URLSearchParams(new URL(inviteB).hash.slice(1)).get("token")!;

  for (const mode of ["existing", "new"] as const) {
    const guest = await newUser(browser);
    const joins: { token: string; new_member_name?: string; claim_member_id?: string }[] = [];
    guest.on("request", (request) => {
      if (new URL(request.url()).pathname === "/api/v1/invites/join") joins.push(request.postDataJSON());
    });
    let release!: () => void;
    let arrived!: () => void;
    const waiting = new Promise<void>((resolve) => { release = resolve; });
    const seen = new Promise<void>((resolve) => { arrived = resolve; });
    await guest.route("**/api/v1/invites/preview", async (route) => {
      if (route.request().postDataJSON().token === tokenB) {
        arrived();
        await waiting;
      }
      await route.continue();
    });
    await guest.goto(inviteA);
    if (mode === "existing") {
      await guest.getByLabel("Bさん", { exact: true }).check();
    } else {
      await guest.getByLabel("新しい名前", { exact: true }).check();
      await guest.getByRole("textbox", { name: "新しい名前" }).fill("古い名前");
    }
    const buttonName = mode === "existing" ? "Bさんとして参加" : "古い名前として参加";
    const oldButton = guest.getByRole("button", { name: buttonName });
    await expect(oldButton).toBeEnabled();
    const attempt = await oldButton.evaluate((button, hash) => {
      const originalFetch = window.fetch;
      const bodies: string[] = [];
      window.fetch = (...args) => {
        if (String(args[0]).endsWith("/api/v1/invites/join")) bodies.push(String(args[1]?.body));
        return originalFetch(...args);
      };
      try {
        location.hash = hash;
        window.dispatchEvent(new HashChangeEvent("hashchange"));
        const connected = button.isConnected;
        (button as HTMLButtonElement).click();
        return { connected, bodies };
      } finally {
        window.fetch = originalFetch;
      }
    }, new URL(inviteB).hash);
    expect(attempt.connected).toBeTruthy();
    expect(attempt.bodies).toEqual([]);
    await seen;
    expect(joins).toEqual([]);
    release();
    await expect(guest.getByText("世代B", { exact: true })).toBeVisible();
    await guest.getByLabel("新しい名前", { exact: true }).check();
    await guest.getByRole("textbox", { name: "新しい名前" }).fill(`新しい名前${mode}`);
    await guest.getByRole("button", { name: `新しい名前${mode}として参加` }).click();
    await expect(guest.getByRole("heading", { name: "世代B", level: 1 })).toBeVisible();
    expect(joins).toHaveLength(1);
    expect(joins[0]).toEqual({ token: tokenB, new_member_name: `新しい名前${mode}` });
    await guest.context().close();
  }
});
