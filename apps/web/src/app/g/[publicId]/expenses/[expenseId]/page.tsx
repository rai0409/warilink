"use client";

import { useParams, useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { ExpenseForm } from "@/components/ExpenseForm";
import { PreviewShares } from "@/components/PreviewShares";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { api, errorMessage, type ExpenseDetail, type Revision } from "@/lib/api";
import { formatYen, SPLIT_MODE_LABEL } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

const CHANGE_LABEL: Record<string, string> = { create: "作成", update: "変更", delete: "削除" };

export default function ExpenseDetailPage() {
  const { group } = useGroup();
  const { expenseId } = useParams<{ expenseId: string }>();
  const router = useRouter();
  const [expense, setExpense] = useState<ExpenseDetail | null>(null);
  const [revisions, setRevisions] = useState<Revision[]>([]);
  const [editing, setEditing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const base = `/groups/${group.public_id}/expenses/${expenseId}`;

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
    if (!expense || !confirm(`「${expense.title}」を削除しますか?`)) return;
    try {
      await api(base, { method: "DELETE", ifMatch: expense.version });
      router.push(`/g/${group.public_id}`);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  if (!expense) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">読み込み中…</p>;
  const names = memberNames(group);
  const onSaved = () => {
    setEditing(false);
    load();
  };

  if (editing) {
    const Editor = expense.split_mode === "itemized" ? ReceiptEditor : ExpenseForm;
    return (
      <>
        <p>
          <button type="button" className="secondary" onClick={() => setEditing(false)}>編集をやめる</button>
        </p>
        <Editor publicId={group.public_id} members={group.members} myMemberId={group.my_member_id}
          editing={expense} onSaved={onSaved} />
      </>
    );
  }

  return (
    <>
      {error && <p className="error" role="alert">{error}</p>}
      <section className="card" aria-labelledby="ex-h">
        <h2 id="ex-h">{expense.title}</h2>
        <p className="big amount">{formatYen(expense.amount_minor)}</p>
        <p className="muted">
          {Object.entries(expense.payers).map(([id, v]) => `${names[id]} ${formatYen(v)}`).join("・")} が立替 ·{" "}
          {SPLIT_MODE_LABEL[expense.split_mode] ?? expense.split_mode} · 版 {expense.version}
        </p>
        <PreviewShares members={group.members} shares={expense.shares} label="確定した負担額" />
        <p className="row actions">
          <button type="button" onClick={() => setEditing(true)}>編集</button>
          <button type="button" className="secondary" onClick={remove}>削除</button>
        </p>
      </section>
      <section className="card" aria-labelledby="rev-h">
        <h2 id="rev-h">変更履歴</h2>
        <ol className="list" aria-label="変更履歴">
          {revisions.map((r) => {
            const amount = (r.spec as { amount_minor?: number }).amount_minor;
            return (
              <li key={r.version}>
                <span>
                  版{r.version} {CHANGE_LABEL[r.change_kind] ?? r.change_kind}
                  {r.changed_by_me && "(あなた)"}
                  <br />
                  <span className="muted">{new Date(r.created_at).toLocaleString("ja-JP")}</span>
                </span>
                <span className="amount">{typeof amount === "number" ? formatYen(amount) : ""}</span>
              </li>
            );
          })}
        </ol>
      </section>
    </>
  );
}
