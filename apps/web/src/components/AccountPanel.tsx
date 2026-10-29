"use client";

import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import type {
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";
import { useCallback, useEffect, useState } from "react";
import { ErrorText, Muted, OutlineButton, Pill, PrimaryButton, SectionTitle, StatusText, TextButton, TextField } from "@/components/kit";
import { api, errorMessage, type Me, type Passkey, type PasskeyOptions, type Plans, type SessionState } from "@/lib/api";

// ブラウザ・認証器のエラーを、利用者が読んで分かる日本語にする(技術的な名前は出さない)
function passkeyError(e: unknown): string {
  const name = e instanceof Error ? e.name : "";
  if (name === "NotAllowedError" || name === "AbortError") return "Passkey の操作が取り消されたか、時間切れになりました";
  if (name === "InvalidStateError") return "この端末の Passkey は既に登録されています";
  if (name === "NotSupportedError" || name === "SecurityError")
    return "このブラウザまたは端末では Passkey を使えません";
  return errorMessage(e);
}

export function AccountPanel() {
  const [me, setMe] = useState<Me | null>(null);
  const [keys, setKeys] = useState<Passkey[]>([]);
  const [nickname, setNickname] = useState("");
  const [merge, setMerge] = useState(true);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [passkeysAvailable, setPasskeysAvailable] = useState(false);

  const load = useCallback(async () => {
    try {
      const m = await api<Me | null>("/identity/me");
      setMe(m);
      const plans = await api<Plans>("/plans");
      setPasskeysAvailable(plans.passkeys_available);
      setKeys(m && m.kind === "registered" ? await api<Passkey[]>("/auth/passkeys") : []);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn: () => Promise<string | null>) {
    setBusy(true);
    setError(null);
    setMessage(null);
    try {
      setMessage(await fn());
    } catch (e) {
      setError(passkeyError(e));
    } finally {
      setBusy(false);
      await load();
    }
  }

  const register = () =>
    run(async () => {
      if (!me) await api("/identity/guest", { method: "POST" });
      const o = await api<PasskeyOptions>("/auth/passkeys/register/options", {
        method: "POST",
        body: { nickname: nickname.trim() || null },
      });
      const credential = await startRegistration({
        optionsJSON: o.options as unknown as PublicKeyCredentialCreationOptionsJSON,
      });
      await api<SessionState>("/auth/passkeys/register/verify", {
        method: "POST",
        body: { flow_id: o.flow_id, credential, nickname: nickname.trim() || null },
      });
      setNickname("");
      return "Passkey を登録しました。別の端末でもこの Passkey でログインできます";
    });

  const login = () =>
    run(async () => {
      const o = await api<PasskeyOptions>("/auth/passkeys/login/options", { method: "POST" });
      const credential = await startAuthentication({
        optionsJSON: o.options as unknown as PublicKeyCredentialRequestOptionsJSON,
      });
      const st = await api<SessionState>("/auth/passkeys/login/verify", {
        method: "POST",
        body: { flow_id: o.flow_id, credential, merge_current_guest: me?.kind === "guest" && merge },
      });
      const moved = st.merged_groups ? `(この端末のグループ ${st.merged_groups} 件を移しました)` : "";
      return `ログインしました${moved}`;
    });

  const isRegistered = me?.kind === "registered";

  return (
    <>
      <section aria-labelledby="acct-h">
        <SectionTitle><span id="acct-h">現在の状態</span></SectionTitle>
        {me === null ? (
          <p className="text-[14px]">この端末ではまだ利用していません。</p>
        ) : isRegistered ? (
          <p className="flex items-center gap-2 text-[14px]">
            <Pill tone="success">Passkey 登録済み</Pill> Passkey {me.passkey_count} 件
          </p>
        ) : (
          <p className="text-[14px]">
            <Pill>ゲスト(登録なし)</Pill> 割り勘・精算はすべてこのまま使えます。
          </p>
        )}
        <Muted className="mt-2 text-[12px]">
          ゲストの情報はこのブラウザの Cookie だけに結びついています。Cookie を消したり端末を変えたりすると、元に戻せない場合があります。
          {passkeysAvailable
            ? "Passkey を登録すると、別の端末からも同じグループに入れます。メールアドレスや電話番号は使いません。"
            : "V0 ベータではゲストセッションで利用できます。Passkey の新規登録・ログインは提供していません。"}
        </Muted>
        {message && <StatusText>{message}</StatusText>}
        {error && <ErrorText>{error}</ErrorText>}
      </section>

      {passkeysAvailable && (
        <section aria-labelledby="reg-h">
          <SectionTitle><span id="reg-h">{isRegistered ? "Passkey を追加" : "Passkey を登録(任意)"}</span></SectionTitle>
          <TextField id="pk-name" className="mt-2" label="この端末の名前(任意。一覧で見分けるためだけに使います)" value={nickname}
            maxLength={40} onChange={setNickname} />
          <PrimaryButton className="mt-3" onClick={register} disabled={busy}>
            {isRegistered ? "Passkey を追加する" : "Passkey を登録する"}
          </PrimaryButton>
        </section>
      )}

      {isRegistered && (
        <section aria-labelledby="keys-h">
          <SectionTitle><span id="keys-h">登録済みの Passkey</span></SectionTitle>
          <ul aria-label="登録済みの Passkey" className="border-t border-border">
            {keys.map((k) => (
              <li key={k.id} className="flex min-h-14 items-center gap-3 border-b border-border py-2">
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[14px]">{k.nickname}</span>
                  <span className="block text-[12px] text-muted-foreground">
                    登録 {new Date(k.created_at).toLocaleDateString("ja-JP")}
                    {k.backed_up ? "・同期あり" : ""}
                  </span>
                </span>
                <TextButton
                  tone="danger"
                  disabled={busy || keys.length <= 1}
                  onClick={() =>
                    run(async () => {
                      await api(`/auth/passkeys/${k.id}`, { method: "DELETE" });
                      return "Passkey を削除しました";
                    })
                  }
                >
                  削除
                </TextButton>
              </li>
            ))}
          </ul>
          {keys.length <= 1 && <Muted className="mt-2 text-[12px]">最後の Passkey は削除できません。先に別の Passkey を追加してください。</Muted>}
          <div className="mt-3 space-y-2">
            <OutlineButton
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api("/auth/sessions/revoke-others", { method: "POST" });
                  return "この端末以外のログインをすべて解除しました";
                })
              }
            >
              ほかの端末のログインをすべて解除
            </OutlineButton>
            <OutlineButton
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api("/auth/logout", { method: "POST" });
                  return "ログアウトしました";
                })
              }
            >
              ログアウト
            </OutlineButton>
          </div>
        </section>
      )}

      {passkeysAvailable && (
        <section aria-labelledby="login-h">
          <SectionTitle><span id="login-h">Passkey でログイン</span></SectionTitle>
          <Muted className="text-[12px]">別の端末で登録した Passkey(同期された Passkey を含む)でログインします。</Muted>
          {me?.kind === "guest" && (
            <label className="mt-2 flex min-h-11 items-center gap-2 text-[14px]">
              <input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} className="size-5 accent-[var(--primary)]" />
              この端末でゲストとして参加したグループを、ログインするアカウントへ移す
            </label>
          )}
          {isRegistered ? (
            <OutlineButton className="mt-3" onClick={login} disabled={busy}>Passkey でログイン</OutlineButton>
          ) : (
            <PrimaryButton className="mt-3" onClick={login} disabled={busy}>Passkey でログイン</PrimaryButton>
          )}
          <Muted className="mt-2 text-[12px]">
            Passkey をすべて失った場合、アカウントを復元する手段はありません(メール・SMS による復元は行いません)。
          </Muted>
        </section>
      )}
    </>
  );
}
