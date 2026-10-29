import { startAuthentication } from "@simplewebauthn/browser";
import type { PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { ApiProblem, api, type Me, type PasskeyOptions } from "@/lib/api";

/** 本人の再確認(step-up)。Passkey でもう一度確認すると、新しいセッションになる */
export async function stepUp() {
  const o = await api<PasskeyOptions>("/auth/passkeys/login/options", { method: "POST" });
  const credential = await startAuthentication({
    optionsJSON: o.options as unknown as PublicKeyCredentialRequestOptionsJSON,
  });
  await api("/auth/passkeys/login/verify", { method: "POST", body: { flow_id: o.flow_id, credential } });
}

/** 再確認で別のアカウントの Passkey が使われた(この端末のセッションが別の人になった) */
export class IdentityChanged extends Error {}

/**
 * 再確認が必要なら Passkey で確認してから、もう一度だけ実行する。
 * 確認の前後で本人(identity_id)が変わった場合は、元の操作を自動で再実行しない(別の人の名前で変更・共有・削除しない)。
 * 画面での比較は表示の安全のためで、認可はサーバーが現在のセッションの本人で判断する。
 */
export async function withStepUp<T>(fn: () => Promise<T>): Promise<T> {
  try {
    return await fn();
  } catch (e) {
    if (e instanceof ApiProblem && e.code === "STEP_UP_REQUIRED") {
      const before = (await api<Me | null>("/identity/me"))?.identity_id;
      await stepUp();
      const after = (await api<Me | null>("/identity/me"))?.identity_id;
      if (!before || before !== after) throw new IdentityChanged();
      return fn();
    }
    throw e;
  }
}

/** 退会(アカウントの削除)。Passkey 登録済みの方は再確認してから削除する */
export function deleteAccount() {
  return withStepUp(() => api("/account", { method: "DELETE", body: { confirm: true } }));
}
