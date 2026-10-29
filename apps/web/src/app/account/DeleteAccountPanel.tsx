"use client";

import { startAuthentication } from "@simplewebauthn/browser";
import type { PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ApiProblem, api, errorMessage, type Me, type PasskeyOptions } from "@/lib/api";

// 本人の再確認(step-up)。Passkey でもう一度確認すると、新しいセッションになる
async function stepUp() {
  const o = await api<PasskeyOptions>("/auth/passkeys/login/options", { method: "POST" });
  const credential = await startAuthentication({
    optionsJSON: o.options as unknown as PublicKeyCredentialRequestOptionsJSON,
  });
  await api("/auth/passkeys/login/verify", { method: "POST", body: { flow_id: o.flow_id, credential } });
}

/**
 * 退会。元に戻せないことを示して確認してから、サーバーで一度に削除する。
 * Passkey 登録済みの方は、Passkey での再確認が必要(別のアカウントで確認された場合は中止する)。
 */
export function DeleteAccountPanel() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Me | null>("/identity/me").then(setMe).catch(() => setMe(null));
  }, []);

  if (!me) return null;

  async function remove() {
    if (
      !confirm(
        "アカウントを削除すると元に戻せません。参加中のグループからは名前だけが残り、あなたはもう見られなくなります。" +
          "振込先と Passkey も使えなくなります。削除しますか?",
      )
    )
      return;
    setBusy(true);
    setError(null);
    const del = () => api("/account", { method: "DELETE", body: { confirm: true } });
    try {
      try {
        await del();
      } catch (e) {
        if (!(e instanceof ApiProblem && e.code === "STEP_UP_REQUIRED")) throw e;
        const before = me?.identity_id;
        await stepUp();
        const after = (await api<Me | null>("/identity/me"))?.identity_id;
        if (!before || before !== after) {
          setError("別のアカウントの Passkey で確認されたため、削除を中止しました。");
          return;
        }
        await del();
      }
      router.push("/?deleted=1");
    } catch (e) {
      const name = e instanceof Error ? e.name : "";
      setError(name === "NotAllowedError" ? "本人確認が取り消されました" : errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="delete-h">
      <h2 id="delete-h">アカウントの削除(退会)</h2>
      <p className="muted">
        削除すると、ログイン状態と Passkey は使えなくなり、振込先は復元できない形で消去されます。参加中のグループでは、
        支出と精算の記録は他のメンバーのために残り、あなたの名前だけのメンバーとして表示されます。
        過去のバックアップに残るデータは、この操作では消えません。
      </p>
      {me.kind === "registered" && <p className="muted">削除の前に Passkey でもう一度確認します。</p>}
      <p className="row actions">
        <button type="button" className="secondary" disabled={busy} onClick={remove}>
          アカウントを削除する
        </button>
      </p>
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
