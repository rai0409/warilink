"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Body, BottomBar, ConfirmDialog, ErrorText, Muted, PrimaryButton, Screen, SectionTitle, TextButton, TopBar } from "@/components/kit";
import { CategoryIcon } from "@/components/kit/icons";
import { ExpenseForm } from "@/components/ExpenseForm";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { api, errorMessage, type ExpenseDetail, type Revision } from "@/lib/api";
import { yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

const KIND: Record<string, string> = { create: "追加", update: "変更", delete: "削除" };

function when(iso: string): string {
  const d = new Date(iso);
  const today = new Date();
  const hm = d.toLocaleTimeString("ja-JP", { hour: "2-digit", minute: "2-digit" });
  return d.toDateString() === today.toDateString() ? hm : `${d.getMonth() + 1}月${d.getDate()}日 ${hm}`;
}

function dayLabel(iso: string | null | undefined, fallback: string): string {
  const d = new Date(iso ?? fallback);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/** 支出の詳細と変更履歴。変更履歴は版ごとの負担額の差分だけを短く示す */
export default function ExpenseDetailPage() {
  const { group } = useGroup();
  const { expenseId } = useParams<{ expenseId: string }>();
  const router = useRouter();
  const [expense, setExpense] = useState<ExpenseDetail | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [editing, setEditing] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const pid = group.public_id;
  const base = `/groups/${pid}/expenses/${expenseId}`;
  const names = memberNames(group);

  const load = useCallback(async () => {
    try {
      const [e, r] = await Promise.all([api<ExpenseDetail>(base), api<Revision[]>(`${base}/revisions`)]);
      setExpense(e);
      setRevisions(r);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [base]);

  useEffect(() => {
    load();
  }, [load]);

  async function remove() {
    if (!expense) return;
    try {
      await api(base, { method: "DELETE", ifMatch: expense.version });
      router.push(`/g/${pid}`);
    } catch (e) {
      setConfirmDelete(false);
      setError(errorMessage(e));
    }
  }

  if (!expense)
    return (
      <Screen>
        <TopBar title="支出" back={`/g/${pid}/expenses`} />
        <Body>{error ? <ErrorText>{error}</ErrorText> : <Muted className="mt-6">読み込み中…</Muted>}</Body>
      </Screen>
    );

  if (editing) {
    const done = () => {
      setEditing(false);
      load();
    };
    return expense.split_mode === "itemized" ? (
      <ReceiptEditor publicId={pid} members={group.members} myMemberId={group.my_member_id} editing={expense}
        onSaved={done} backHref={`/g/${pid}/expenses/${expenseId}`} onReloadLatest={done} />
    ) : (
      <ExpenseForm publicId={pid} members={group.members} myMemberId={group.my_member_id} editing={expense}
        onSaved={done} backHref={`/g/${pid}/expenses/${expenseId}`} onReloadLatest={done} />
    );
  }

  const spec = expense.spec as { category?: string | null; occurred_on?: string | null };
  const payers = Object.keys(expense.payers).map((id) => names[id]).join("・");
  const ordered = [...revisions].sort((a, b) => b.version - a.version);

  return (
    <Screen>
      <TopBar title="支出" back={`/g/${pid}/expenses`} />
      <Body>
        <div className="mt-4 flex items-center gap-4 border-b border-border pb-5">
          <CategoryIcon category={spec.category} className="size-6 shrink-0" />
          <div className="min-w-0 flex-1">
            <h2 className="truncate text-[16px] font-semibold">{expense.title}</h2>
            <p className="text-[12px] text-muted-foreground">
              {dayLabel(spec.occurred_on, expense.created_at)} · {payers}が支払い
            </p>
          </div>
          <span className="amount text-[18px] font-semibold tabular-nums">{yen(expense.amount_minor)}</span>
        </div>

        <SectionTitle>負担額</SectionTitle>
        <ul aria-label="確定した負担額" className="text-[14px]">
          {group.members.filter((m) => expense.shares[m.id] !== undefined).map((m) => (
            <li key={m.id} className="flex justify-between border-b border-border py-3">
              <span>{m.display_name}</span>
              <span className="amount tabular-nums">{yen(expense.shares[m.id])}</span>
            </li>
          ))}
        </ul>

        <SectionTitle>変更履歴</SectionTitle>
        <ol aria-label="変更履歴" className="relative mt-2 ml-1 border-l border-border pl-5">
          {ordered.map((r, i) => {
            const prev = ordered[i + 1];
            const now = ((r.result as { shares?: Record<string, number> }).shares ?? {}) as Record<string, number>;
            const before = ((prev?.result as { shares?: Record<string, number> } | undefined)?.shares ?? {}) as Record<string, number>;
            const diffs = prev ? Object.keys({ ...now, ...before }).filter((id) => (now[id] ?? 0) !== (before[id] ?? 0)) : [];
            const who = r.changed_by_me ? "あなた" : (r.changed_by_name ?? "退会したメンバー");
            return (
              <li key={r.version} className="relative pb-6">
                <span aria-hidden className={`absolute top-1.5 -left-[25px] size-2.5 rounded-full ${i === 0 ? "bg-primary" : "bg-[#b6bec9]"}`} />
                <p className="text-[12px] text-muted-foreground">{when(r.created_at)}</p>
                <p className="text-[14px] font-medium">
                  {who}が{r.change_kind === "update" && diffs.length > 0 ? "負担額を変更" : (KIND[r.change_kind] ?? r.change_kind)}
                </p>
                {diffs.length > 0 && (
                  <ul className="mt-1 border-l border-border pl-3 text-[13px]">
                    {diffs.map((id) => (
                      <li key={id} className="grid grid-cols-[4em_1fr] gap-2 tabular-nums">
                        <span>{names[id] ?? "—"}</span>
                        <span>{yen(before[id] ?? 0)} → {yen(now[id] ?? 0)}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </li>
            );
          })}
        </ol>
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      {!group.closed && (
        <BottomBar>
          <PrimaryButton onClick={() => setEditing(true)}>編集する</PrimaryButton>
          <TextButton tone="danger" className="w-full justify-center" onClick={() => setConfirmDelete(true)}>
            この支出を削除
          </TextButton>
        </BottomBar>
      )}
      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title={`「${expense.title}」を削除しますか?`}
        description="削除した支出は精算の計算から外れます。履歴は残ります。" confirmLabel="削除する" danger onConfirm={remove} />
    </Screen>
  );
}
