"use client";

import { useState } from "react";
import { OutlineButton, ErrorText, Muted, TextButton } from "@/components/kit";
import { api, errorMessage, type PayerDestination, type TransferRow } from "@/lib/api";

const TYPE_LABEL: Record<string, string> = { ordinary: "普通", checking: "当座", savings: "貯蓄" };

/** 支払者だけに表示する振込先。アプリは送金しない(表示とコピーのみ) */
export function DestinationView({ transfer }: { transfer: TransferRow }) {
  const [d, setD] = useState<PayerDestination | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
    } catch {
      setError("コピーできませんでした。手動で入力してください");
    }
  }

  if (!d) {
    return (
      <>
        <OutlineButton className="mt-3"
          onClick={() => api<PayerDestination>(`/settlements/${transfer.id}/destination`).then(setD).catch((e) => setError(errorMessage(e)))}>
          振込先を表示
        </OutlineButton>
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
      {error && <ErrorText>{error}</ErrorText>}
    </section>
  );
}
