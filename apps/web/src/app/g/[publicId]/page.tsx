"use client";

import { useCallback, useEffect, useState } from "react";
import { ArrowLeftRight, ChartColumn, Link2, ListFilter, Settings, UsersRound } from "lucide-react";
import { Amount, Body, BottomBar, ButtonLink, ErrorText, List, Muted, Row, Screen, SectionTitle, TopBar } from "@/components/kit";
import { CategoryIcon } from "@/components/kit/icons";
import { api, errorMessage, type Expense, type SettlementPlan } from "@/lib/api";
import { yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

/** グループのホーム。自分の状況(金額は 1 つだけ)・最近の支出・各画面への入口 */
export default function GroupHomePage() {
  const { group } = useGroup();
  const pid = group.public_id;
  const [expenses, setExpenses] = useState<Expense[] | null>(null);
  const [plan, setPlan] = useState<SettlementPlan | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const [ex, p] = await Promise.all([
        api<Expense[]>(`/groups/${pid}/expenses`),
        api<SettlementPlan>(`/groups/${pid}/settlements/preview`, { method: "POST" }),
      ]);
      setExpenses(ex);
      setPlan(p);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [pid]);

  useEffect(() => {
    load();
  }, [load]);

  const names = memberNames(group);
  const myNet = plan?.balances.find((b) => b.member_id === group.my_member_id)?.net_minor ?? 0;
  const base = `/g/${pid}`;

  return (
    <Screen>
      <TopBar title={group.title} subtitle={`${group.members.length}人${group.closed ? " · 終了" : ""}`} back="/" />
      <Body>
        {error && <ErrorText>{error}</ErrorText>}
        <Amount
          label={myNet > 0 ? "受け取る予定" : myNet < 0 ? "支払う予定" : "あなたの精算"}
          value={myNet === 0 ? "精算なし" : yen(Math.abs(myNet))}
        />
        <SectionTitle>最近の支出</SectionTitle>
        {expenses?.length === 0 && <Muted className="py-4">まだ支出はありません</Muted>}
        <List aria-label="支出">
          {(expenses ?? []).slice(0, 3).map((e) => (
            <Row
              key={e.id}
              href={`${base}/expenses/${e.id}`}
              icon={<CategoryIcon category={e.category} />}
              label={e.title}
              sub={`${Object.keys(e.payers).map((id) => names[id]).join("・")}が支払い`}
              value={<span className="amount text-[16px] font-semibold text-foreground tabular-nums">{yen(e.amount_minor)}</span>}
            />
          ))}
        </List>
        <SectionTitle>グループ</SectionTitle>
        <List aria-label="グループのメニュー">
          <Row href={`${base}/expenses`} icon={<ListFilter strokeWidth={1.6} />} label="支出" value={expenses ? `${expenses.length}件` : ""} />
          <Row href={`${base}/stats`} icon={<ChartColumn strokeWidth={1.6} />} label="集計" />
          <Row href={`${base}/settle`} icon={<ArrowLeftRight strokeWidth={1.6} />} label="精算"
            value={plan && plan.transfers.length > 0 ? `${plan.transfers.length}件の送金` : ""} />
          <Row href={`${base}/members`} icon={<UsersRound strokeWidth={1.6} />} label="メンバー" value={`${group.members.length}人`} />
          {group.is_owner && <Row href={`${base}/invite`} icon={<Link2 strokeWidth={1.6} />} label="招待リンク" />}
          <Row href={`${base}/settings`} icon={<Settings strokeWidth={1.6} />} label="グループ設定" />
        </List>
      </Body>
      {!group.closed && (
        <BottomBar>
          <ButtonLink href={`${base}/expenses/new`}>支出を追加</ButtonLink>
        </BottomBar>
      )}
    </Screen>
  );
}
