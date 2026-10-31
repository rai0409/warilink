"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { OutlineButton, ErrorText, Muted, TextButton } from "@/components/kit";
import { api, errorMessage, type Me, type PayerDestination, type TransferRow } from "@/lib/api";

const TYPE_LABEL: Record<string, string> = { ordinary: "普通", checking: "当座", savings: "貯蓄" };

/** 表示中の振込先。どの送金・どの本人のために取得したかを一緒に持つ */
type Shown = { transferId: string; identityId: string; data: PayerDestination };

/**
 * 支払者だけに表示する振込先。アプリは送金しない(表示とコピーのみ)。
 * 口座の詳細はこのコンポーネントの state にだけ置き、URL・localStorage・ログには残さない。
 * 前の表示が残らないように:
 * - 取得した送金 ID と本人が、いま表示している送金・本人と一致するときだけ表示する
 * - 送金が変わったら・画面を離れたら、表示と取得中の要求を捨てる(遅れて届いた古い応答は使わない)
 * - 画面に戻ったときにログイン状態を確かめ、本人が変わっていたら表示を閉じる
 * 認可はサーバーが支払者本人かどうかで判断する(ここでの比較は表示の安全のため)。
 */
export function DestinationView({ transfer }: { transfer: TransferRow }) {
  const [shown, setShown] = useState<Shown | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const request = useRef(0); // 進行中の取得の番号。変わったら古い応答を捨てる

  const clear = useCallback((message: string | null = null) => {
    request.current += 1;
    setShown(null);
    setCopied(null);
    setNotice(message);
  }, []);

  // 送金が変わったら、前の送金の表示と取得中の要求を捨てる
  useEffect(() => {
    clear();
    setError(null);
    return () => {
      request.current += 1;
    };
  }, [transfer.id, clear]);

  // 別のタブでログアウト・別のアカウントでログインした場合に備え、画面に戻ったときに本人を確かめる
  useEffect(() => {
    if (!shown) return;
    const recheck = () => {
      if (document.visibilityState !== "visible") return;
      api<Me | null>("/identity/me")
        .then((me) => {
          if (me?.identity_id !== shown.identityId) clear("ログイン状態が変わったため、振込先の表示を閉じました");
        })
        .catch(() => clear("ログイン状態を確認できないため、振込先の表示を閉じました"));
    };
    window.addEventListener("focus", recheck);
    document.addEventListener("visibilitychange", recheck);
    return () => {
      window.removeEventListener("focus", recheck);
      document.removeEventListener("visibilitychange", recheck);
    };
  }, [shown, clear]);

  async function reveal() {
    const mine = ++request.current;
    const transferId = transfer.id;
    setError(null);
    setNotice(null);
    try {
      const me = await api<Me | null>("/identity/me");
      const data = await api<PayerDestination>(`/settlements/${transferId}/destination`);
      if (mine !== request.current || !me) return; // 送金の切り替え・画面の移動のあとに届いた応答
      setShown({ transferId, identityId: me.identity_id, data });
    } catch (e) {
      if (mine === request.current) setError(errorMessage(e));
    }
  }

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
    } catch {
      setError("コピーできませんでした。手動で入力してください");
    }
  }

  const d = shown && shown.transferId === transfer.id ? shown.data : null;
  if (!d) {
    return (
      <>
        <OutlineButton className="mt-3" onClick={reveal}>
          振込先を表示
        </OutlineButton>
        {notice && <Muted className="mt-2" role="status">{notice}</Muted>}
        {error && <ErrorText>{error}</ErrorText>}
      </>
    );
  }
  if (!d.available || !d.details) {
    return <Muted className="mt-3">受取人は振込先を共有していません。受取人に直接確認してください。</Muted>;
  }
  const x = d.details as Record<string, string>;
  const rows: [string, string][] =
    d.kind === "bank"
      ? [
          ["銀行", `${x.bank_name}(${x.bank_code})`],
          ["支店", `${x.branch_name}(${x.branch_code})`],
          ["種類", TYPE_LABEL[x.account_type] ?? x.account_type],
          ["口座番号", x.account_number],
          ["名義", x.holder_kana],
        ]
      : [["PayPay ID", x.paypay_id]];
  rows.push(["照合コード", d.reference_code ?? ""]);
  return (
    <section aria-label="振込先" className="mt-4">
      {d.changed_since_first_view && (
        <p role="alert" className="mb-2 text-[13px] text-warning">
          受取人が振込先を変更しました。以前に表示した振込先({d.first_viewed_masked_label})ではなく、下の振込先を確認してください。
        </p>
      )}
      <dl className="border-t border-border text-[14px]">
        {rows.map(([k, v]) => (
          <div key={k} className="flex min-h-12 items-center gap-3 border-b border-border">
            <dt className="w-20 shrink-0 text-[13px] text-muted-foreground">{k}</dt>
            <dd className="min-w-0 flex-1 truncate">{v}</dd>
            {(k === "口座番号" || k === "PayPay ID" || k === "照合コード") && (
              <TextButton onClick={() => copy(k, v)}>{copied === k ? "コピーしました" : `${k}をコピー`}</TextButton>
            )}
          </div>
        ))}
      </dl>
      <Muted className="mt-2 text-[12px]">{d.notice}</Muted>
      <TextButton tone="muted" className="mt-1" onClick={() => clear()}>振込先を隠す</TextButton>
      {error && <ErrorText>{error}</ErrorText>}
    </section>
  );
}
