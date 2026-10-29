"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, Search, X } from "lucide-react";
import { Body, BottomBar, ButtonLink, ErrorText, List, Muted, OutlineButton, PrimaryButton, Row, Screen, Sheet, TextButton, TopBar } from "@/components/kit";
import { CategoryIcon } from "@/components/kit/icons";
import { api, errorMessage, type ExpenseSearch } from "@/lib/api";
import { CATEGORY_LABEL, yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";
import { cn } from "@/lib/utils";

type Filters = { q: string; category: string; from: string; to: string; payer: string; mine: boolean; sort: string };
const EMPTY: Filters = { q: "", category: "", from: "", to: "", payer: "", mine: false, sort: "date_desc" };
const SORT_LABEL: Record<string, string> = {
  date_desc: "新しい順",
  date_asc: "古い順",
  amount_desc: "金額の大きい順",
  amount_asc: "金額の小さい順",
};

function query(f: Filters, cursor?: string | null): string {
  const p = new URLSearchParams();
  if (f.q.trim()) p.set("q", f.q.trim());
  if (f.category === "none") p.set("uncategorized", "true");
  else if (f.category) p.set("category", f.category);
  if (f.from) p.set("date_from", f.from);
  if (f.to) p.set("date_to", f.to);
  if (f.payer) p.set("payer_member_id", f.payer);
  if (f.mine) p.set("created_by_me", "true");
  p.set("sort", f.sort);
  if (cursor) p.set("cursor", cursor);
  return p.toString();
}

function day(occurredOn: string | null | undefined, createdAt: string): string {
  const d = occurredOn ? new Date(`${occurredOn}T00:00:00`) : new Date(createdAt);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

const short = (d: string) => (d ? `${Number(d.slice(5, 7))}/${Number(d.slice(8, 10))}` : "");

/** グループ内の支出を探す(このグループの支出だけが対象) */
export default function ExpensesPage() {
  const { group } = useGroup();
  const names = memberNames(group);
  const pid = group.public_id;
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [draft, setDraft] = useState<Filters>(EMPTY);
  const [q, setQ] = useState("");
  const [sheet, setSheet] = useState(false);
  const [result, setResult] = useState<ExpenseSearch | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (filters: Filters, cursor?: string | null) => {
      try {
        const r = await api<ExpenseSearch>(`/groups/${pid}/expenses/search?${query(filters, cursor)}`);
        setResult((prev) => (cursor && prev ? { ...r, items: [...prev.items, ...r.items] } : r));
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    [pid],
  );

  useEffect(() => {
    load(applied);
  }, [load, applied]);

  // 文字の検索は入力が止まってから反映する
  useEffect(() => {
    const t = setTimeout(() => setApplied((a) => (a.q === q ? a : { ...a, q })), 300);
    return () => clearTimeout(t);
  }, [q]);

  const chip = (label: string, active: boolean) => (
    <button type="button" onClick={() => { setDraft(applied); setSheet(true); }}
      className={cn("inline-flex min-h-10 items-center gap-1 rounded-lg border px-3 text-[13px]",
        active ? "border-primary bg-secondary text-secondary-foreground" : "border-input text-foreground")}>
      {label}
      <ChevronDown className="size-4" aria-hidden />
    </button>
  );
  const setD = <K extends keyof Filters>(k: K, v: Filters[K]) => setDraft({ ...draft, [k]: v });

  return (
    <Screen>
      <TopBar title="支出" back={`/g/${pid}`} />
      <Body>
        <div role="search" className="relative mt-2">
          <Search className="pointer-events-none absolute top-1/2 left-3 size-5 -translate-y-1/2 text-muted-foreground" aria-hidden />
          <input aria-label="内容で検索" type="search" value={q} maxLength={100} onChange={(e) => setQ(e.target.value)}
            placeholder="内容で検索" className="h-11 w-full rounded-lg bg-muted pr-10 pl-10 text-[15px] outline-none focus-visible:ring-[3px] focus-visible:ring-ring/30" />
          {q && (
            <button type="button" aria-label="検索の文字を消す" onClick={() => setQ("")}
              className="absolute top-1/2 right-1 grid size-10 -translate-y-1/2 place-items-center text-muted-foreground">
              <X className="size-4" aria-hidden />
            </button>
          )}
        </div>
        <div className="mt-3 flex gap-2" aria-label="絞り込み">
          {chip(applied.category ? (applied.category === "none" ? "未分類" : CATEGORY_LABEL[applied.category]) : "カテゴリ", Boolean(applied.category))}
          {chip(applied.payer ? names[applied.payer] : "立替者", Boolean(applied.payer))}
          {chip(applied.from || applied.to ? `${short(applied.from)}—${short(applied.to)}` : "期間", Boolean(applied.from || applied.to))}
        </div>
        {result && (
          <div className="mt-4 flex items-center justify-between text-[13px] text-muted-foreground">
            <span role="status">{result.total_count}件 · {yen(result.total_amount_minor)}</span>
            <label className="flex items-center gap-1">
              <span className="sr-only">並び順</span>
              <select aria-label="並び順" value={applied.sort} onChange={(e) => setApplied({ ...applied, sort: e.target.value })}
                className="bg-transparent text-right text-[13px] text-muted-foreground">
                {Object.entries(SORT_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
              </select>
            </label>
          </div>
        )}
        {result?.items.length === 0 && <Muted className="py-6">条件に合う支出はありません</Muted>}
        <List aria-label="検索結果" className="mt-2">
          {(result?.items ?? []).map((e) => (
            <Row key={e.id} href={`/g/${pid}/expenses/${e.id}`} icon={<CategoryIcon category={e.category} />} label={e.title}
              sub={`${day(e.occurred_on, e.created_at)} · ${Object.keys(e.payers).map((id) => names[id]).join("、")}が支払い`}
              value={<span className="amount text-[16px] font-semibold text-foreground tabular-nums">{yen(e.amount_minor)}</span>} />
          ))}
        </List>
        {result?.next_cursor && (
          <OutlineButton className="mt-4" onClick={() => load(applied, result.next_cursor)}>さらに表示</OutlineButton>
        )}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      {!group.closed && (
        <BottomBar>
          <ButtonLink href={`/g/${pid}/expenses/new`}>支出を追加</ButtonLink>
        </BottomBar>
      )}

      <Sheet open={sheet} onOpenChange={setSheet} title="絞り込み"
        footer={
          <div className="flex items-center gap-4">
            <TextButton onClick={() => setDraft({ ...EMPTY, q: applied.q, sort: applied.sort })}>リセット</TextButton>
            <PrimaryButton className="flex-1" onClick={() => { setApplied({ ...draft, q: applied.q }); setSheet(false); }}>適用する</PrimaryButton>
          </div>
        }>
        <p className="mt-4 text-[13px] text-muted-foreground">期間</p>
        <div className="mt-2 flex items-center gap-2">
          <input aria-label="この日から" type="date" value={draft.from} onChange={(e) => setD("from", e.target.value)}
            className="h-11 min-w-0 flex-1 appearance-none rounded-md border border-input bg-background px-3 text-left text-[14px] leading-[42px] [&::-webkit-date-and-time-value]:text-left" />
          <span aria-hidden>—</span>
          <input aria-label="この日まで" type="date" value={draft.to} onChange={(e) => setD("to", e.target.value)}
            className="h-11 min-w-0 flex-1 appearance-none rounded-md border border-input bg-background px-3 text-left text-[14px] leading-[42px] [&::-webkit-date-and-time-value]:text-left" />
        </div>
        <p className="mt-5 text-[13px] text-muted-foreground" id="payer-label">立替者</p>
        <div role="group" aria-labelledby="payer-label" className="mt-2 flex flex-wrap gap-2">
          {group.members.map((m) => (
            <button key={m.id} type="button" aria-pressed={draft.payer === m.id}
              onClick={() => setD("payer", draft.payer === m.id ? "" : m.id)}
              className="min-h-10 rounded-lg bg-muted px-4 text-[13px] aria-pressed:bg-secondary aria-pressed:text-secondary-foreground">
              {m.display_name}
            </button>
          ))}
        </div>
        <label className="mt-5 block text-[13px] text-muted-foreground">
          カテゴリ
          <select aria-label="カテゴリ" value={draft.category} onChange={(e) => setD("category", e.target.value)}
            className="mt-2 h-11 w-full rounded-md border border-input bg-background px-3 text-[14px] text-foreground">
            <option value="">すべて</option>
            <option value="none">未分類</option>
            {Object.entries(CATEGORY_LABEL).map(([v, l]) => <option key={v} value={v}>{l}</option>)}
          </select>
        </label>
        <label className="mt-4 flex min-h-11 items-center gap-2 text-[14px]">
          <input type="checkbox" checked={draft.mine} onChange={(e) => setD("mine", e.target.checked)} className="size-5 accent-[var(--primary)]" />
          自分が登録した支出だけ
        </label>
      </Sheet>
    </Screen>
  );
}
