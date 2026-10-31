"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ArrowRight, ChevronRight, Copy } from "lucide-react";
import { Amount, Body, ErrorText, Muted, Pill, PrimaryButton, Screen, SectionTitle, StatusText, TextButton, TopBar } from "@/components/kit";
import { DestinationView } from "@/components/DestinationView";
import { api, errorMessage, type Batch, type Plans, type TransferRow } from "@/lib/api";
import { yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";
import { statusPill } from "@/lib/settle";

/** 1件の送金。支払う人には「送金の記録」、受け取る人には「受け取りの確認」として見せる */
export default function TransferPage() {
  const { group } = useGroup();
  const { transferId } = useParams<{ transferId: string }>();
  const pid = group.public_id;
  const me = group.my_member_id;
  const all = memberNames(group);
  const name = (id: string) => (id === me ? "あなた" : (all[id] ?? "退会したメンバー"));
  const [batch, setBatch] = useState<Batch | null>(null);
  const [destinations, setDestinations] = useState(false);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [b, flags] = await Promise.all([api<Batch | null>(`/groups/${pid}/settlement-batches/current`), api<Plans>("/plans")]);
      setBatch(b);
      setDestinations(flags.payment_destinations_available);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [pid]);

  useEffect(() => {
    load();
  }, [load]);

  const t = batch?.transfers.find((x) => x.id === transferId);

  async function act(path: string, message: string) {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      await api(`/settlements/${transferId}/${path}`, { method: "POST" });
      setDone(message);
    } catch (e) {
      setError(errorMessage(e));
    }
    setBusy(false);
    await load();
  }

  async function copy(v: TransferRow) {
    try {
      await navigator.clipboard.writeText(String(v.amount_minor));
      setCopied(true);
    } catch {
      setError("コピーできませんでした。金額を手動で入力してください");
    }
  }

  if (!t) {
    return (
      <Screen>
        <TopBar title="送金" subtitle={group.title} back={`/g/${pid}/settle`} />
        <Body>
          {error ? <ErrorText>{error}</ErrorText> : batch === null ? <Muted className="mt-6">読み込み中…</Muted> : (
            <Muted className="mt-6">この送金は見つかりません。精算案が更新された可能性があります。</Muted>
          )}
        </Body>
      </Screen>
    );
  }

  const payer = t.from_member_id === me;
  const recipient = t.to_member_id === me;
  const s = statusPill(t);
  const others = (batch?.transfers ?? []).filter(
    (x) => x.id !== t.id && x.status !== "superseded" && (payer ? x.from_member_id === me : recipient ? x.to_member_id === me : false),
  );
  const title = recipient ? "受け取りの確認" : "送金の記録";

  return (
    <Screen>
      <TopBar title={title} subtitle={group.title} back={`/g/${pid}/settle`} />
      <Body>
        <div className="mt-6">
          <Pill tone={s.tone}>{s.label}</Pill>
        </div>
        <p className="mt-4 flex items-center gap-4 text-[16px]">
          <span>{name(t.from_member_id)}</span>
          <ArrowRight className="size-4 text-muted-foreground" aria-label="から" />
          <span>{name(t.to_member_id)}</span>
        </p>
        <div className="flex items-end justify-between">
          <Amount value={yen(t.amount_minor)} className="mt-1" />
          {payer && t.status === "unpaid" && (
            <TextButton onClick={() => copy(t)} aria-label="金額をコピー">
              <Copy className="size-4" aria-hidden />
              {copied ? "コピーしました" : "コピー"}
            </TextButton>
          )}
        </div>

        {payer && t.status === "unpaid" && (
          <>
            <Muted className="mt-4">いつもの方法で送金し、申告してください。</Muted>
            <PrimaryButton className="mt-6" disabled={busy} onClick={() => act("report-paid", "送金したと申告しました")}>
              送金したと申告
            </PrimaryButton>
          </>
        )}
        {payer && t.status === "reported_paid" && <Muted className="mt-4">受け取る人の確認を待っています。</Muted>}
        {destinations && payer && (t.status === "unpaid" || t.status === "reported_paid") && <DestinationView key={t.id} transfer={t} />}

        {recipient && t.status === "reported_paid" && (
          <>
            <Muted className="mt-4">入金を確認してから記録してください。</Muted>
            <PrimaryButton className="mt-6" disabled={busy} onClick={() => act("confirm-received", "受け取りを記録しました")}>
              受け取った
            </PrimaryButton>
            <TextButton className="mt-3 w-full justify-center" disabled={busy}
              onClick={() => act("not-received", "まだ受け取っていないことを記録しました")}>
              まだ受け取っていない
            </TextButton>
          </>
        )}
        {recipient && t.status === "unpaid" && (
          <>
            <Muted className="mt-4">送金の申告はまだありません。入金を確認できた場合は記録できます。</Muted>
            <PrimaryButton className="mt-6" disabled={busy} onClick={() => act("confirm-received", "受け取りを記録しました")}>
              受け取った
            </PrimaryButton>
          </>
        )}
        {t.status === "recipient_confirmed" && <Muted className="mt-4">受け取る人が受け取りを記録しました。</Muted>}
        {done && <StatusText>{done}</StatusText>}
        {error && <ErrorText>{error}</ErrorText>}

        {others.length > 0 && (
          <section className="mt-10 border-t border-border pt-2">
            <SectionTitle>{recipient ? "ほかの受け取り" : "ほかの送金"}</SectionTitle>
            <ul>
              {others.map((o) => {
                const os = statusPill(o);
                return (
                  <li key={o.id} className="border-b border-border">
                    <Link href={`/g/${pid}/settle/${o.id}`} className="flex items-center gap-3 py-3 text-foreground no-underline">
                      <span className="min-w-0 flex-1">
                        <Pill tone={os.tone}>{os.label}</Pill>
                        <span className="mt-2 block text-[14px]">
                          {recipient ? `${name(o.from_member_id)}から` : `${name(o.to_member_id)}へ`}
                        </span>
                        <span className="amount block text-[16px] font-semibold tabular-nums">{yen(o.amount_minor)}</span>
                      </span>
                      <ChevronRight className="size-4 text-muted-foreground" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
          </section>
        )}
        <Muted className="mt-6 text-[12px]">WariLinkは送金を実行しません。</Muted>
      </Body>
    </Screen>
  );
}
