"use client";

import { useCallback, useEffect, useState } from "react";
import { ConfirmDialog, ErrorText, Muted, OutlineButton, PrimaryButton, SectionTitle, StatusText, TextButton } from "@/components/kit";
import { api, errorMessage, type Destination, type DestinationReveal, type Me, type Plans } from "@/lib/api";
import { IdentityChanged, withStepUp } from "@/lib/stepup";

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
  const [confirmDelete, setConfirmDelete] = useState(false);
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
      <section aria-labelledby="dest-h">
        <SectionTitle><span id="dest-h">受取口座(振込先)</span></SectionTitle>
        <p className="text-[14px]">振込先の保存は、Passkey を登録した方だけが使えます。ゲストのままでも割り勘と精算はすべて使えます。</p>
      </section>
    );
  }

  const set = (k: keyof typeof EMPTY) => (e: { target: { value: string } }) => setForm({ ...form, [k]: e.target.value });
  const field = (id: string, label: string, k: keyof typeof EMPTY, extra: React.ComponentProps<"input"> = {}) => (
    <div className="mt-4">
      <label htmlFor={id} className="mb-2 block text-[13px] text-muted-foreground">{label}</label>
      <input id={id} value={form[k]} onChange={set(k)} required {...extra}
        className="h-12 w-full rounded-md border border-input bg-background px-4 text-[15px] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30" />
    </div>
  );
  const shareBox = (checked: boolean, onChange: (v: boolean) => void) => (
    <label className="mt-3 flex min-h-11 items-center gap-2 text-[14px]">
      <input type="checkbox" checked={checked} onChange={(e) => onChange(e.target.checked)} className="size-5 accent-[var(--primary)]" />
      あなたに支払う精算の相手に表示する
    </label>
  );

  return (
    <section aria-labelledby="dest-h">
      <SectionTitle><span id="dest-h">受取口座(振込先)</span></SectionTitle>
      {destinationsAvailable ? (
        <Muted className="text-[12px]">
          暗号化して保存し、あなたに支払う精算の相手にだけ表示します(共有を許可した場合)。このアプリは送金を実行せず、銀行へのログイン情報も扱いません。
          登録・変更・全桁の表示には、Passkey での確認が必要です。
        </Muted>
      ) : (
        <Muted className="text-[12px]">V0 ベータでは新しい振込先を保存・共有できません。保存済みデータは削除できます。</Muted>
      )}
      {dest && !editing && (
        <>
          <p className="mt-3 text-[14px]">
            <strong>{dest.masked_label}</strong>(第{dest.version}版)
          </p>
          {destinationsAvailable &&
            shareBox(dest.share_with_payers, (checked) =>
              run(async () => {
                await withStepUpIfAvailable(() => api("/account/payment-destination/sharing", {
                  method: "POST",
                  body: { share_with_payers: checked },
                }));
                return checked ? "支払う相手への表示を許可しました" : "支払う相手への表示を取り消しました";
              }),
            )}
          {revealed && (
            <dl aria-label="振込先の全桁" className="mt-3 grid grid-cols-[4em_1fr] gap-x-3 gap-y-1 text-[14px]">
              <dt className="text-muted-foreground">銀行</dt>
              <dd>{String(revealed.details.bank_name)}({String(revealed.details.bank_code)})</dd>
              <dt className="text-muted-foreground">支店</dt>
              <dd>{String(revealed.details.branch_name)}({String(revealed.details.branch_code)})</dd>
              <dt className="text-muted-foreground">口座</dt>
              <dd>{TYPE_LABEL[String(revealed.details.account_type)]} {String(revealed.details.account_number)}</dd>
              <dt className="text-muted-foreground">名義</dt>
              <dd>{String(revealed.details.holder_kana)}</dd>
            </dl>
          )}
          <div className="mt-3 space-y-2">
            {destinationsAvailable && (
              <OutlineButton
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
              </OutlineButton>
            )}
            {destinationsAvailable && (
              <OutlineButton onClick={() => { setEditing(true); setShare(dest.share_with_payers); }}>変更する</OutlineButton>
            )}
            <TextButton tone="danger" className="w-full justify-center" onClick={() => setConfirmDelete(true)}>削除する</TextButton>
          </div>
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
          {field("d-bank-code", "銀行コード(4桁)", "bank_code", { inputMode: "numeric", maxLength: 4 })}
          {field("d-bank-name", "銀行名", "bank_name", { maxLength: 30 })}
          {field("d-branch-code", "支店コード(3桁)", "branch_code", { inputMode: "numeric", maxLength: 3 })}
          {field("d-branch-name", "支店名", "branch_name", { maxLength: 30 })}
          <div className="mt-4">
            <label htmlFor="d-type" className="mb-2 block text-[13px] text-muted-foreground">口座の種類</label>
            <select id="d-type" value={form.account_type} onChange={set("account_type")}
              className="h-12 w-full rounded-md border border-input bg-background px-3 text-[15px]">
              <option value="ordinary">普通</option>
              <option value="checking">当座</option>
              <option value="savings">貯蓄</option>
            </select>
          </div>
          {field("d-number", "口座番号(7桁)", "account_number", { inputMode: "numeric", maxLength: 7 })}
          {field("d-holder", "口座名義(カタカナ)", "holder_kana", { maxLength: 30 })}
          {shareBox(share, setShare)}
          <PrimaryButton type="submit" className="mt-4">{dest ? "この内容で変更" : "保存する"}</PrimaryButton>
          {editing && (
            <TextButton tone="muted" className="mt-1 w-full justify-center" onClick={() => setEditing(false)}>やめる</TextButton>
          )}
        </form>
      )}
      {message && <StatusText>{message}</StatusText>}
      {error && <ErrorText>{error}</ErrorText>}
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title="振込先を削除しますか?"
        description="削除すると、元に戻せません。" confirmLabel="削除する" danger
        onConfirm={() => {
          setConfirmDelete(false);
          run(async () => {
            await withStepUpIfAvailable(() => api("/account/payment-destination", { method: "DELETE" }));
            setRevealed(null);
            return "振込先を削除しました";
          });
        }} />
    </section>
  );
}
