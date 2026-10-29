"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { ArrowRight, ChevronRight } from "lucide-react";
import { Body, BottomBar, ErrorText, Muted, Pill, PrimaryButton, Screen, SectionTitle, TopBar } from "@/components/kit";
import { api, errorMessage, type Batch, type SettlementPlan } from "@/lib/api";
import { yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";
import { statusPill } from "@/lib/settle";

function Pair({ from, to }: { from: string; to: string }) {
  return (
    <span className="flex items-center gap-4 text-[14px]">
      <span>{from}</span>
      <ArrowRight className="size-4 text-muted-foreground" aria-label="から" />
      <span>{to}</span>
    </span>
  );
}

/** 精算案。確定前は案を、確定後は送金ごとの状態を示す */
export default function SettlePage() {
  const { group } = useGroup();
  const pid = group.public_id;
  const me = group.my_member_id;
  const all = memberNames(group);
  const name = (id: string) => (id === me ? "あなた" : (all[id] ?? "退会したメンバー"));
  const [plan, setPlan] = useState<SettlementPlan | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [p, b] = await Promise.all([
        api<SettlementPlan>(`/groups/${pid}/settlements/preview`, { method: "POST" }),
        api<Batch | null>(`/groups/${pid}/settlement-batches/current`),
      ]);
      setPlan(p);
      setBatch(b);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [pid]);

  useEffect(() => {
    load();
  }, [load]);

  async function confirm() {
    if (!plan) return;
    setBusy(true);
    setError(null);
    try {
      await api(`/groups/${pid}/settlement-batches`, { method: "POST", body: { expected_ledger_version: plan.ledger_version } });
    } catch (e) {
      setError(errorMessage(e));
    }
    setBusy(false);
    await load();
  }

  const opt = plan?.optimization;
  const showPlan = plan && (!batch || batch.is_stale);
  const transfers = batch && !batch.is_stale ? batch.transfers.filter((t) => t.status !== "superseded") : [];
  const count = showPlan ? plan.transfers.length : transfers.length;

  return (
    <Screen>
      <TopBar title="精算案" subtitle={group.title} back={`/g/${pid}`} />
      <Body>
        {(plan || batch) && (
          <>
            <h2 className="mt-6 text-[16px] font-semibold">
              {count === 0 ? "精算は必要ありません" : `${count}回の送金で精算`}
            </h2>
            {opt && count > 0 && (
              <Muted className="mt-1">
                {opt.is_exact_minimum ? "送金回数が最少になる組み合わせです。" : "送金回数を減らした組み合わせです。"}
              </Muted>
            )}
            {batch?.is_stale && (
              <p role="status" className="mt-2 text-[13px] text-warning">確定後に支出が変更されました。精算案を確定し直してください。</p>
            )}
          </>
        )}

        {showPlan && plan.transfers.length > 0 && (
          <ul aria-label="精算案" className="mt-4 border-t border-border">
            {plan.transfers.map((t, i) => (
              <li key={i} className="border-b border-border py-4">
                <Pair from={name(t.from_member_id)} to={name(t.to_member_id)} />
                <p className="amount mt-1 text-[16px] font-semibold tabular-nums">{yen(t.amount_minor)}</p>
              </li>
            ))}
          </ul>
        )}

        {transfers.length > 0 && (
          <>
            <ul aria-label="確定した精算" className="mt-4 border-t border-border">
              {transfers.map((t) => {
                const s = statusPill(t);
                return (
                  <li key={t.id} className="border-b border-border">
                    <Link href={`/g/${pid}/settle/${t.id}`} className="flex items-center gap-3 py-4 text-foreground no-underline">
                      <span className="min-w-0 flex-1">
                        <Pair from={name(t.from_member_id)} to={name(t.to_member_id)} />
                        <span className="amount mt-1 block text-[16px] font-semibold tabular-nums">{yen(t.amount_minor)}</span>
                      </span>
                      <Pill tone={s.tone}>{s.label}</Pill>
                      <ChevronRight className="size-4 text-muted-foreground" aria-hidden />
                    </Link>
                  </li>
                );
              })}
            </ul>
            {batch && batch.progress.reported_unconfirmed_count > 0 && (
              <Muted className="mt-3 text-[12px]">
                「送金の申告あり」は支払う人の申告だけで、受け取る人はまだ確認していません。
              </Muted>
            )}
          </>
        )}

        {opt && (
          <details className="mt-8 text-[12px] text-muted-foreground">
            <summary className="cursor-pointer py-2">計算の前提</summary>
            <ul className="list-disc space-y-1 pl-5">
              <li>誰から誰へでも送金できるものとして計算しています</li>
              <li>1円単位の整数で計算し、振込手数料は考慮していません</li>
              <li>申告済み・受け取り済みの送金は変更せず、残りだけを計算し直します</li>
              {!opt.is_exact_minimum && <li>人数が多いため、送金回数が最少であることは保証していません</li>}
            </ul>
          </details>
        )}
        <SectionTitle className="sr-only">送金について</SectionTitle>
        <Muted className="mt-2 text-[12px]">WariLinkは送金を実行しません。</Muted>
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      {showPlan && plan.transfers.length > 0 && !group.closed && (
        <BottomBar>
          <PrimaryButton disabled={busy} onClick={confirm}>精算案を確定</PrimaryButton>
        </BottomBar>
      )}
    </Screen>
  );
}
