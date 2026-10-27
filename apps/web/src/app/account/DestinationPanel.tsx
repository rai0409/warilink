"use client";

import { startAuthentication } from "@simplewebauthn/browser";
import type { PublicKeyCredentialRequestOptionsJSON } from "@simplewebauthn/browser";
import { useCallback, useEffect, useState } from "react";
import {
  api,
  ApiProblem,
  errorMessage,
  type Destination,
  type DestinationReveal,
  type Me,
  type PasskeyOptions,
  type Plans,
} from "@/lib/api";

const TYPE_LABEL: Record<string, string> = { ordinary: "普通", checking: "当座", savings: "貯蓄" };
const EMPTY = {
  bank_code: "",
  bank_name: "",
  branch_code: "",
  branch_name: "",
  account_type: "ordinary",
  account_number: "",
  holder_kana: "",
};

// 本人の再確認(step-up)。Passkey でもう一度確認すると、新しいセッションになる
async function stepUp() {
  const o = await api<PasskeyOptions>("/auth/passkeys/login/options", { method: "POST" });
  const credential = await startAuthentication({
    optionsJSON: o.options as unknown as PublicKeyCredentialRequestOptionsJSON,
  });
  await api("/auth/passkeys/login/verify", { method: "POST", body: { flow_id: o.flow_id, credential } });
}

/** 再確認で別のアカウントの Passkey が使われた(この端末のセッションが別の人になった) */
class IdentityChanged extends Error {}

/**
 * 再確認が必要なら Passkey で確認してから、もう一度だけ実行する。
 * 確認の前後で本人(identity_id)が変わった場合は、元の操作を自動で再実行しない(別の人の名前で変更・共有・削除しない)。
 * 画面での比較は表示の安全のためで、認可はサーバーが現在のセッションの本人で判断する。
 */
async function withStepUp<T>(fn: () => Promise<T>): Promise<T> {
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

export function DestinationPanel() {
  const [me, setMe] = useState<Me | null>(null);
  const [dest, setDest] = useState<Destination | null>(null);
  const [revealed, setRevealed] = useState<DestinationReveal | null>(null);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState(EMPTY);
  const [share, setShare] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [loaded, setLoaded] = useState(false);
  const [destinationsAvailable, setDestinationsAvailable] = useState(false);
  const [passkeysAvailable, setPasskeysAvailable] = useState(false);

  const load = useCallback(async () => {
    try {
      // 両方を取得してから同時に反映する(途中の状態で入力欄が一瞬表示されるのを防ぐ)
      const m = await api<Me | null>("/identity/me");
      const plans = await api<Plans>("/plans");
      setDestinationsAvailable(plans.payment_destinations_available);
      setPasskeysAvailable(plans.passkeys_available);
      const d = m?.kind === "registered" ? await api<Destination | null>("/account/payment-destination") : null;
      setMe(m);
      setDest(d);
      setLoaded(true);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn: () => Promise<string>) {
    setError(null);
    setMessage(null);
    try {
      setMessage(await fn());
    } catch (e) {
      if (e instanceof IdentityChanged) {
        // 別のアカウントになった: 表示中の全桁と入力途中の内容を消し、元の操作は行わない
        setRevealed(null);
        setForm(EMPTY);
        setEditing(false);
        setError("別のアカウントの Passkey で確認されたため、操作を中止しました。表示中の内容は閉じました。");
      } else {
        const name = e instanceof Error ? e.name : "";
        setError(name === "NotAllowedError" ? "本人確認が取り消されました" : errorMessage(e));
      }
    }
    await load();
  }

  // Passkey の機能が使えるときは、機密操作(削除を含む)の前に Passkey で再確認する。
  // Passkey の機能を止めている間だけ、API は所有者による既存データの消去を再確認なしで受け付ける(API と同じ条件)。
  async function withStepUpIfAvailable<T>(fn: () => Promise<T>): Promise<T> {
    return passkeysAvailable ? withStepUp(fn) : fn();
  }

  if (!loaded) return null;
  if (!destinationsAvailable && !dest) return null;
  if (me?.kind !== "registered") {
    return (
      <section className="card" aria-labelledby="dest-h">
        <h2 id="dest-h">受取口座(振込先)</h2>
        <p>振込先の保存は、Passkey を登録した方だけが使えます。ゲストのままでも割り勘と精算はすべて使えます。</p>
      </section>
    );
  }

  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });

  return (
    <section className="card" aria-labelledby="dest-h">
      <h2 id="dest-h">受取口座(振込先)</h2>
      {destinationsAvailable ? <p className="muted">
        暗号化して保存し、あなたに支払う精算の相手にだけ表示します(共有を許可した場合)。このアプリは送金を実行せず、銀行へのログイン情報も扱いません。
        登録・変更・全桁の表示には、Passkey での確認が必要です。
      </p> : <p className="muted">V0 ベータでは新しい振込先を保存・共有できません。保存済みデータは削除できます。</p>}
      {dest && !editing && (
        <>
          <p>
            <strong>{dest.masked_label}</strong>(第{dest.version}版)
          </p>
          {destinationsAvailable && <label className="row">
            <input
              type="checkbox"
              checked={dest.share_with_payers}
              onChange={(e) => {
                // 制御された入力は await の後に元の値へ戻るため、先に値を取っておく
                const checked = e.target.checked;
                run(async () => {
                  await withStepUpIfAvailable(() => api("/account/payment-destination/sharing", {
                    method: "POST",
                    body: { share_with_payers: checked },
                  }));
                  return checked ? "支払う相手への表示を許可しました" : "支払う相手への表示を取り消しました";
                });
              }}
            />
            あなたに支払う精算の相手に表示する
          </label>}
          {revealed && (
            <dl aria-label="振込先の全桁">
              <dt>銀行</dt>
              <dd>
                {String(revealed.details.bank_name)}({String(revealed.details.bank_code)})
              </dd>
              <dt>支店</dt>
              <dd>
                {String(revealed.details.branch_name)}({String(revealed.details.branch_code)})
              </dd>
              <dt>口座</dt>
              <dd>
                {TYPE_LABEL[String(revealed.details.account_type)]} {String(revealed.details.account_number)}
              </dd>
              <dt>名義</dt>
              <dd>{String(revealed.details.holder_kana)}</dd>
            </dl>
          )}
          <p className="row actions">
            {destinationsAvailable && <button
              type="button"
              className="secondary"
              onClick={() =>
                run(async () => {
                  setRevealed(
                    revealed
                      ? null
                      : await withStepUpIfAvailable(() => api<DestinationReveal>("/account/payment-destination/reveal")),
                  );
                  return revealed ? "全桁の表示を閉じました" : "全桁を表示しました";
                })
              }
            >
              {revealed ? "全桁を隠す" : "全桁を表示"}
            </button>}
            {destinationsAvailable && <button
              type="button"
              className="secondary"
              onClick={() => {
                setEditing(true);
                setShare(dest.share_with_payers);
              }}
            >
              変更する
            </button>}
            <button
              type="button"
              className="secondary"
              onClick={() =>
                run(async () => {
                  // 削除は元に戻せないため、必ず確認する(取り消したら何もしない)
                  if (!confirm("振込先を削除すると、元に戻せません。削除しますか?")) return "削除を取り消しました";
                  await withStepUpIfAvailable(() => api("/account/payment-destination", { method: "DELETE" }));
                  setRevealed(null);
                  return "振込先を削除しました";
                })
              }
            >
              削除する
            </button>
          </p>
        </>
      )}
      {destinationsAvailable && (!dest || editing) && (
        <form
          onSubmit={(e) => {
            e.preventDefault();
            run(async () => {
              const body = { kind: "bank", bank: form, share_with_payers: share };
              await withStepUpIfAvailable(() =>
                api("/account/payment-destination", { method: dest ? "PUT" : "POST", body }),
              );
              setEditing(false);
              setForm(EMPTY);
              setRevealed(null);
              return dest ? "振込先を変更しました。以前に表示した相手には変更の注意を表示します" : "振込先を登録しました";
            });
          }}
        >
          <label htmlFor="d-bank-code">銀行コード(4桁)</label>
          <input id="d-bank-code" inputMode="numeric" maxLength={4} value={form.bank_code} onChange={set("bank_code")} required />
          <label htmlFor="d-bank-name">銀行名</label>
          <input id="d-bank-name" maxLength={30} value={form.bank_name} onChange={set("bank_name")} required />
          <label htmlFor="d-branch-code">支店コード(3桁)</label>
          <input id="d-branch-code" inputMode="numeric" maxLength={3} value={form.branch_code} onChange={set("branch_code")} required />
          <label htmlFor="d-branch-name">支店名</label>
          <input id="d-branch-name" maxLength={30} value={form.branch_name} onChange={set("branch_name")} required />
          <label htmlFor="d-type">口座の種類</label>
          <select id="d-type" value={form.account_type} onChange={set("account_type")}>
            <option value="ordinary">普通</option>
            <option value="checking">当座</option>
            <option value="savings">貯蓄</option>
          </select>
          <label htmlFor="d-number">口座番号(7桁)</label>
          <input id="d-number" inputMode="numeric" maxLength={7} value={form.account_number} onChange={set("account_number")} required />
          <label htmlFor="d-holder">口座名義(カタカナ)</label>
          <input id="d-holder" maxLength={30} value={form.holder_kana} onChange={set("holder_kana")} required />
          <label className="row">
            <input type="checkbox" checked={share} onChange={(e) => setShare(e.target.checked)} />
            あなたに支払う精算の相手に表示する
          </label>
          <p className="row actions">
            <button type="submit">{dest ? "この内容で変更" : "保存する"}</button>
            {editing && (
              <button type="button" className="secondary" onClick={() => setEditing(false)}>
                やめる
              </button>
            )}
          </p>
        </form>
      )}
      {message && <p className="notice" role="status">{message}</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </section>
  );
}
