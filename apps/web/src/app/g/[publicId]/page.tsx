"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { InviteBox } from "@/components/InviteBox";
import { api, errorMessage, type Expense, type SettlementPlan } from "@/lib/api";
import { formatYen, SPLIT_MODE_LABEL } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

export default function GroupOverviewPage() {
  const { group } = useGroup();
  const publicId = group.public_id;
  const [expenses, setExpenses] = useState<Expense[] | null>(null);
  const [plan, setPlan] = useState<SettlementPlan | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ex, p] = await Promise.all([
        api<Expense[]>(`/groups/${publicId}/expenses`),
        api<SettlementPlan>(`/groups/${publicId}/settlements/preview`, { method: "POST" }),
      ]);
      setExpenses(ex);
      setPlan(p);
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [publicId]);

  useEffect(() => {
    load();
  }, [load]);

  const names = memberNames(group);
  const net = Object.fromEntries((plan?.balances ?? []).map((b) => [b.member_id, b.net_minor]));
  const total = (expenses ?? []).reduce((s, e) => s + e.amount_minor, 0);
  const myNet = net[group.my_member_id] ?? 0;

  return (
    <>
      {error && <p className="error" role="alert">{error}</p>}
      <section className="card" aria-labelledby="me-h">
        <h2 id="me-h">あなたの状況</h2>
        <p className="big amount">
          {myNet > 0 ? `${formatYen(myNet)} 受け取る予定` : myNet < 0 ? `${formatYen(-myNet)} 支払う予定` : "精算の必要はありません"}
        </p>
        {plan && plan.transfers.length > 0 && (
          <p>
            <Link href={`/g/${publicId}/settle`}>精算の詳細を見る</Link>
          </p>
        )}
      </section>

      <section className="card" aria-labelledby="members-h">
        <h2 id="members-h">メンバーと残高</h2>
        <ul className="list">
          {group.members.map((m) => (
            <li key={m.id}>
              <span>
                {m.display_name}
                {m.is_me && "(あなた)"} {!m.claimed && <span className="muted">未参加</span>}
              </span>
              <span className="amount" data-testid={`net-${m.display_name}`}>
                {formatYen(net[m.id] ?? 0)}
              </span>
            </li>
          ))}
        </ul>
        <p className="muted">プラスは受け取る額、マイナスは支払う額です。合計支出 {formatYen(total)}</p>
        {group.is_owner && <InviteBox publicId={publicId} />}
      </section>

      <section className="card" aria-labelledby="expenses-h">
        <h2 id="expenses-h">支出一覧</h2>
        <p className="row">
          <Link className="button" href={`/g/${publicId}/expenses/new`}>支出を追加</Link>
          <Link className="button secondary" href={`/g/${publicId}/receipt`}>レシートから商品別に追加</Link>
        </p>
        {expenses?.length === 0 && <p className="muted">まだ支出はありません</p>}
        <ul className="list" aria-label="支出">
          {(expenses ?? []).map((e) => (
            <li key={e.id}>
              <span>
                <Link href={`/g/${publicId}/expenses/${e.id}`}>{e.title}</Link>
                <br />
                <span className="muted">
                  {Object.keys(e.payers).map((id) => names[id]).join("・")}が立替 · {SPLIT_MODE_LABEL[e.split_mode] ?? e.split_mode}
                </span>
              </span>
              <span className="amount">{formatYen(e.amount_minor)}</span>
            </li>
          ))}
        </ul>
      </section>
    </>
  );
}
