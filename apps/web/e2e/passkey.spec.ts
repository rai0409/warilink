import AxeBuilder from "@axe-core/playwright";
import { expect, test, type Page } from "@playwright/test";
import { createGroup, newUser, openTab } from "./helpers";

// Chromium の仮想認証器(CDP WebAuthn)で Passkey を作り、別の端末(別のブラウザコンテキスト)へ同じ Passkey を移してログインする。
// 実在の認証器・外部サービスは使わない。

async function virtualAuthenticator(page: Page) {
  const cdp = await page.context().newCDPSession(page);
  await cdp.send("WebAuthn.enable");
  const { authenticatorId } = await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: {
      protocol: "ctap2",
      transport: "internal",
      hasResidentKey: true,
      hasUserVerification: true,
      isUserVerified: true,
      automaticPresenceSimulation: true,
    },
  });
  return { cdp, authenticatorId };
}

test("ゲストのまま使い始め、Passkey を登録して別の端末からログインすると同じグループに入れる", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "Passkey旅行", "Bさん");
  await openTab(a, "支出を追加");
  await a.getByLabel("内容").fill("ホテル");
  await a.getByLabel("金額(円)").fill("12,000");
  await a.getByRole("button", { name: "この内容で登録" }).click();
  await expect(a.getByRole("list", { name: "支出" })).toContainText("ホテル");
  const groupUrl = a.url();

  // 端末 A: ゲストから Passkey を登録(任意)
  const devA = await virtualAuthenticator(a);
  await a.goto("/account");
  await expect(a.getByText("ゲスト(登録なし)")).toBeVisible();
  const axe = await new AxeBuilder({ page: a }).withTags(["wcag2a", "wcag2aa"]).analyze();
  expect(axe.violations).toEqual([]);
  await a.getByLabel(/この端末の名前/).fill("テスト用の端末");
  await a.getByRole("button", { name: "Passkey を登録する" }).click();
  await expect(a.getByRole("status")).toContainText("Passkey を登録しました");
  await expect(a.getByText("Passkey 登録済み")).toBeVisible();
  await expect(a.getByRole("list", { name: "登録済みの Passkey" })).toContainText("テスト用の端末");
  // 昇格後も同じグループ・支出が見える
  await a.goto(groupUrl);
  await expect(a.getByRole("list", { name: "支出" })).toContainText("ホテル");

  // 端末 B: Cookie の無い別のブラウザ。同期された Passkey を模して同じクレデンシャルを入れる
  const { credentials } = await devA.cdp.send("WebAuthn.getCredentials", { authenticatorId: devA.authenticatorId });
  expect(credentials).toHaveLength(1);
  const b = await newUser(browser, { baseURL });
  await b.goto(groupUrl);
  await expect(b.getByRole("heading", { name: "Passkey旅行" })).toHaveCount(0); // ログイン前は見えない
  const devB = await virtualAuthenticator(b);
  await devB.cdp.send("WebAuthn.addCredential", { authenticatorId: devB.authenticatorId, credential: credentials[0] });
  await b.goto("/account");
  await b.getByRole("button", { name: "Passkey でログイン" }).click();
  await expect(b.getByRole("status")).toContainText("ログインしました");
  await b.goto(groupUrl);
  await expect(b.getByRole("heading", { name: "Passkey旅行", level: 1 })).toBeVisible();
  await expect(b.getByRole("list", { name: "支出" })).toContainText("ホテル");

  // ログアウト後は見えない
  await b.goto("/account");
  await b.getByRole("button", { name: "ログアウト" }).click();
  await expect(b.getByRole("status")).toContainText("ログアウトしました");
  await b.goto(groupUrl);
  await expect(b.getByRole("heading", { name: "Passkey旅行" })).toHaveCount(0);
});

test("Passkey を作れない端末では、日本語で理由を表示して状態は変わらない", async ({ browser, baseURL }) => {
  const a = await newUser(browser, { baseURL });
  await createGroup(a, "取り消し", "Bさん");
  // ユーザー検証(生体認証・PIN)も端末内保存もできない認証器 → サーバーの要件を満たせず、ブラウザが拒否する
  const cdp = await a.context().newCDPSession(a);
  await cdp.send("WebAuthn.enable");
  await cdp.send("WebAuthn.addVirtualAuthenticator", {
    options: { protocol: "ctap2", transport: "usb", hasResidentKey: false, hasUserVerification: false,
      automaticPresenceSimulation: true },
  });
  await a.goto("/account");
  await a.getByRole("button", { name: "Passkey を登録する" }).click();
  const err = a.locator("p.error[role=alert]");
  await expect(err).toContainText(/取り消され|時間切れ|使えません|登録されています/, { timeout: 30_000 });
  await expect(err).not.toContainText(/Error|webauthn|NotAllowed/i); // 技術的な文字列は出さない
  await expect(a.getByText("ゲスト(登録なし)")).toBeVisible();
});
