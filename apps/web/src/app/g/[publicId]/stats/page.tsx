"use client";

import { useCallback, useEffect, useState } from "react";
import { ChevronDown, ChevronRight } from "lucide-react";
import { Amount, Body, ErrorText, Muted, Screen, SectionTitle, TopBar } from "@/components/kit";
import { CategoryIcon } from "@/components/kit/icons";
import { api, errorMessage, type GroupStats } from "@/lib/api";
import { categoryLabel, yen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

type Period = "all" | "this" | "last";

function range(p: Period): [string, string] {
  if (p === "all") return ["", ""];
  const now = new Date();
  const y = now.getFullYear();
  const m = now.getMonth() + (p === "this" ? 0 : -1);
  const first = new Date(y, m, 1);
  const last = new Date(y, m + 1, 0);
  const iso = (d: Date) => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  return [iso(first), iso(last)];
}

/** グループ内の集計。立て替えた額と負担額を混同しないよう別の列で示す */
export default function GroupStatsPage() {
  const { group } = useGroup();
  const names = memberNames(group);
  const [period, setPeriod] = useState<Period>("all");
  const [stats, setStats] = useState<GroupStats | null>(null);
  const [months, setMonths] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async (p: Period) => {
    const [f, t] = range(p);
    const q = new URLSearchParams();
    if (f) q.set("date_from", f);
    if (t) q.set("date_to", t);
    try {
      setStats(await api<GroupStats>(`/groups/${group.public_id}/expenses/stats?${q}`));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [group.public_id]);

  useEffect(() => {
    load(period);
  }, [load, period]);

  const maxCat = Math.max(1, ...(stats?.by_category ?? []).map((c) => c.total_minor));

  return (
    <Screen>
      <TopBar title="集計" back={`/g/${group.public_id}`} />
      <Body>
        <label className="mt-2 inline-flex">
          <span className="sr-only">期間</span>
          <select aria-label="期間" value={period} onChange={(e) => setPeriod(e.target.value as Period)}
            className="h-10 rounded-lg border border-input bg-background px-3 text-[13px]">
            <option value="all">全期間</option>
            <option value="this">今月</option>
            <option value="last">先月</option>
          </select>
        </label>
        {stats && (
          <>
            <Amount label="支出合計" value={yen(stats.total_minor)} />
            <Muted>支出 {stats.expense_count}件</Muted>
            {stats.unconfirmed_transfer_count > 0 && (
              <p className="mt-2 text-[13px] text-warning">
                送金申告済み・受取確認待ち {stats.unconfirmed_transfer_count}件({yen(stats.unconfirmed_transfer_minor)})
              </p>
            )}
            <table aria-label="メンバー別の集計" className="mt-6 w-full border-t border-border text-[14px]">
              <thead>
                <tr className="border-b border-border">
                  <th scope="col" className="py-3 text-left font-medium">メンバー</th>
                  <th scope="col" className="py-3 text-right font-normal text-muted-foreground">立替</th>
                  <th scope="col" className="py-3 text-right font-normal text-muted-foreground">負担</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_member.map((m) => (
                  <tr key={m.member_id} className="border-b border-border">
                    <th scope="row" className="py-3 text-left font-normal">{names[m.member_id]}</th>
                    <td className="amount py-3 text-right tabular-nums">{yen(m.paid_minor)}</td>
                    <td className="amount py-3 text-right tabular-nums">{yen(m.share_minor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            <SectionTitle>カテゴリ</SectionTitle>
            {stats.by_category.length === 0 ? (
              <Muted className="py-3">この期間の支出はありません</Muted>
            ) : (
              <ul aria-label="カテゴリ別の集計">
                {stats.by_category.map((c) => (
                  <li key={c.category ?? "none"} className="flex min-h-12 items-center gap-3">
                    <CategoryIcon category={c.category} />
                    <span className="w-16 text-[14px]">{categoryLabel(c.category)}</span>
                    <span aria-hidden className="h-2 flex-1 overflow-hidden rounded-full bg-muted">
                      <span className="block h-full rounded-full bg-primary" style={{ width: `${(c.total_minor / maxCat) * 100}%` }} />
                    </span>
                    <span className="amount w-20 text-right text-[14px] tabular-nums">{yen(c.total_minor)}</span>
                  </li>
                ))}
              </ul>
            )}
            <div className="mt-4 border-y border-border">
              <button type="button" aria-expanded={months} onClick={() => setMonths(!months)}
                className="flex min-h-14 w-full items-center justify-between text-[14px]">
                月別の集計
                {months ? <ChevronDown className="size-4" aria-hidden /> : <ChevronRight className="size-4 text-muted-foreground" aria-hidden />}
              </button>
              {months && (
                <ul aria-label="月別の集計" className="pb-3">
                  {stats.by_month.map((m) => (
                    <li key={m.month} className="flex justify-between py-1.5 text-[14px]">
                      <span>{m.month.replace("-", "年")}月 <span className="text-[12px] text-muted-foreground">{m.count}件</span></span>
                      <span className="amount tabular-nums">{yen(m.total_minor)}</span>
                    </li>
                  ))}
                </ul>
              )}
            </div>
            <ul className="mt-6 list-disc space-y-1 pl-5 text-[12px] text-muted-foreground">
              {stats.notes.map((n) => <li key={n}>{n}</li>)}
              <li>この集計はグループのメンバーへの表示のためだけに計算しています(広告や外部への提供には使いません)</li>
            </ul>
          </>
        )}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
    </Screen>
  );
}
