"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, type ExpenseSearch } from "@/lib/api";
import { CATEGORY_LABEL, categoryLabel, formatYen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

type Filters = { q: string; category: string; from: string; to: string; payer: string; mine: boolean; sort: string };
const EMPTY: Filters = { q: "", category: "", from: "", to: "", payer: "", mine: false, sort: "date_desc" };

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

/** グループ内の支出を探す(このグループの支出だけが対象) */
export default function ExpenseSearchPage() {
  const { group } = useGroup();
  const names = memberNames(group);
  const [f, setF] = useState<Filters>(EMPTY);
  const [applied, setApplied] = useState<Filters>(EMPTY);
  const [result, setResult] = useState<ExpenseSearch | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (filters: Filters, cursor?: string | null) => {
      try {
        const r = await api<ExpenseSearch>(`/groups/${group.public_id}/expenses/search?${query(filters, cursor)}`);
        setResult((prev) => (cursor && prev ? { ...r, items: [...prev.items, ...r.items] } : r));
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    [group.public_id],
  );

  useEffect(() => {
    load(applied);
  }, [load, applied]);

  const set = <K extends keyof Filters>(k: K) => (v: Filters[K]) => setF({ ...f, [k]: v });

  return (
    <>
      <section className="card" aria-labelledby="search-h">
        <h2 id="search-h">支出を探す</h2>
        <form
          role="search"
          onSubmit={(e) => {
            e.preventDefault();
            setApplied(f);
          }}
        >
          <label htmlFor="s-q">内容で検索</label>
          <input id="s-q" type="search" value={f.q} maxLength={100} onChange={(e) => set("q")(e.target.value)} />
          <div className="row">
            <span>
              <label htmlFor="s-cat">カテゴリ</label>
              <select id="s-cat" value={f.category} onChange={(e) => set("category")(e.target.value)}>
                <option value="">すべて</option>
                <option value="none">未分類</option>
                {Object.entries(CATEGORY_LABEL).map(([v, l]) => (
                  <option key={v} value={v}>{l}</option>
                ))}
              </select>
            </span>
            <span>
              <label htmlFor="s-payer">立て替えた人</label>
              <select id="s-payer" value={f.payer} onChange={(e) => set("payer")(e.target.value)}>
                <option value="">全員</option>
                {group.members.map((m) => (
                  <option key={m.id} value={m.id}>{m.display_name}</option>
                ))}
              </select>
            </span>
          </div>
          <div className="row">
            <span>
              <label htmlFor="s-from">この日から</label>
              <input id="s-from" type="date" value={f.from} onChange={(e) => set("from")(e.target.value)} />
            </span>
            <span>
              <label htmlFor="s-to">この日まで</label>
              <input id="s-to" type="date" value={f.to} onChange={(e) => set("to")(e.target.value)} />
            </span>
          </div>
          <label className="check">
            <input type="checkbox" checked={f.mine} onChange={(e) => set("mine")(e.target.checked)} /> 自分が登録した支出だけ
          </label>
          <label htmlFor="s-sort">並び順</label>
          <select id="s-sort" value={f.sort} onChange={(e) => set("sort")(e.target.value)}>
            <option value="date_desc">日付の新しい順</option>
            <option value="date_asc">日付の古い順</option>
            <option value="amount_desc">金額の大きい順</option>
            <option value="amount_asc">金額の小さい順</option>
          </select>
          <p className="row actions">
            <button type="submit">検索</button>
            <button
              type="button"
              className="secondary"
              onClick={() => {
                setF(EMPTY);
                setApplied(EMPTY);
              }}
            >
              条件をクリア
            </button>
          </p>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      {result && (
        <section className="card" aria-labelledby="result-h">
          <h2 id="result-h">
            {result.total_count}件(合計 {formatYen(result.total_amount_minor)})
          </h2>
          <p className="muted">合計は支出(立て替えた額)の合計です。各自の負担額は「集計」で確認できます。</p>
          {result.items.length === 0 ? (
            <p className="muted">条件に合う支出はありません</p>
          ) : (
            <ul className="list" aria-label="検索結果">
              {result.items.map((e) => (
                <li key={e.id}>
                  <span>
                    <Link href={`/g/${group.public_id}/expenses/${e.id}`}>{e.title}</Link>
                    <br />
                    <span className="muted">
                      {e.occurred_on ?? new Date(e.created_at).toLocaleDateString("ja-JP")}・{categoryLabel(e.category)}・
                      {Object.keys(e.payers).map((id) => names[id]).join("、")}が立替
                    </span>
                  </span>
                  <span className="amount">{formatYen(e.amount_minor)}</span>
                </li>
              ))}
            </ul>
          )}
          {result.next_cursor && (
            <p>
              <button type="button" className="secondary" onClick={() => load(applied, result.next_cursor)}>
                さらに表示
              </button>
            </p>
          )}
        </section>
      )}
    </>
  );
}
