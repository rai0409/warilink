"use client";

import { startAuthentication, startRegistration } from "@simplewebauthn/browser";
import type {
  PublicKeyCredentialCreationOptionsJSON,
  PublicKeyCredentialRequestOptionsJSON,
} from "@simplewebauthn/browser";
import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, type Me, type Passkey, type PasskeyOptions, type SessionState } from "@/lib/api";

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

  const load = useCallback(async () => {
    try {
      const m = await api<Me | null>("/identity/me");
      setMe(m);
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
      <section className="card" aria-labelledby="acct-h">
        <h2 id="acct-h">現在の状態</h2>
        {me === null ? (
          <p>この端末ではまだ利用していません。</p>
        ) : isRegistered ? (
          <p>
            <span className="chip exact">Passkey 登録済み</span> Passkey {me.passkey_count} 件
          </p>
        ) : (
          <p>
            <span className="chip">ゲスト(登録なし)</span> 割り勘・精算はすべてこのまま使えます。
          </p>
        )}
        <p className="muted">
          ゲストの情報はこのブラウザの Cookie だけに結びついています。Cookie を消したり端末を変えたりすると、元に戻せない場合があります。
          Passkey を登録すると、別の端末からも同じグループに入れます。メールアドレスや電話番号は使いません。
        </p>
        {message && <p className="notice" role="status">{message}</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="card" aria-labelledby="reg-h">
        <h2 id="reg-h">{isRegistered ? "Passkey を追加" : "Passkey を登録(任意)"}</h2>
        <label htmlFor="pk-name">この端末の名前(任意。一覧で見分けるためだけに使います)</label>
        <input id="pk-name" value={nickname} maxLength={40} onChange={(e) => setNickname(e.target.value)} />
        <p className="row actions">
          <button type="button" onClick={register} disabled={busy}>
            {isRegistered ? "Passkey を追加する" : "Passkey を登録する"}
          </button>
        </p>
      </section>

      {isRegistered && (
        <section className="card" aria-labelledby="keys-h">
          <h2 id="keys-h">登録済みの Passkey</h2>
          <ul className="list" aria-label="登録済みの Passkey">
            {keys.map((k) => (
              <li key={k.id}>
                <span>
                  {k.nickname}
                  <br />
                  <span className="muted">
                    登録 {new Date(k.created_at).toLocaleDateString("ja-JP")}
                    {k.backed_up ? "・同期あり" : ""}
                  </span>
                </span>
                <button
                  type="button"
                  className="secondary"
                  disabled={busy || keys.length <= 1}
                  onClick={() =>
                    run(async () => {
                      await api(`/auth/passkeys/${k.id}`, { method: "DELETE" });
                      return "Passkey を削除しました";
                    })
                  }
                >
                  削除
                </button>
              </li>
            ))}
          </ul>
          {keys.length <= 1 && <p className="muted">最後の Passkey は削除できません。先に別の Passkey を追加してください。</p>}
          <p className="row actions">
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api("/auth/sessions/revoke-others", { method: "POST" });
                  return "この端末以外のログインをすべて解除しました";
                })
              }
            >
              ほかの端末のログインをすべて解除
            </button>
            <button
              type="button"
              className="secondary"
              disabled={busy}
              onClick={() =>
                run(async () => {
                  await api("/auth/logout", { method: "POST" });
                  return "ログアウトしました";
                })
              }
            >
              ログアウト
            </button>
          </p>
        </section>
      )}

      <section className="card" aria-labelledby="login-h">
        <h2 id="login-h">Passkey でログイン</h2>
        <p className="muted">別の端末で登録した Passkey(同期された Passkey を含む)でログインします。</p>
        {me?.kind === "guest" && (
          <label className="row">
            <input type="checkbox" checked={merge} onChange={(e) => setMerge(e.target.checked)} />
            この端末でゲストとして参加したグループを、ログインするアカウントへ移す
          </label>
        )}
        <p className="row actions">
          <button type="button" className={isRegistered ? "secondary" : ""} onClick={login} disabled={busy}>
            Passkey でログイン
          </button>
        </p>
        <p className="muted">
          Passkey をすべて失った場合、アカウントを復元する手段はありません(メール・SMS による復元は行いません)。
        </p>
      </section>
    </>
  );
}
