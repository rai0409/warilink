"use client";

import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, type GroupStats } from "@/lib/api";
import { categoryLabel, formatYen } from "@/lib/format";
import { memberNames, useGroup } from "@/lib/group-context";

/** グループ内の集計。立て替えた額と負担額を混同しないよう別の列で示す */
export default function GroupStatsPage() {
  const { group } = useGroup();
  const names = memberNames(group);
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const [stats, setStats] = useState<GroupStats | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(
    async (f: string, t: string) => {
      const p = new URLSearchParams();
      if (f) p.set("date_from", f);
      if (t) p.set("date_to", t);
      try {
        setStats(await api<GroupStats>(`/groups/${group.public_id}/expenses/stats?${p}`));
        setError(null);
      } catch (e) {
        setError(errorMessage(e));
      }
    },
    [group.public_id],
  );

  useEffect(() => {
    load("", "");
  }, [load]);

  return (
    <>
      <section className="card" aria-labelledby="period-h">
        <h2 id="period-h">期間</h2>
        <form
          className="row"
          onSubmit={(e) => {
            e.preventDefault();
            load(from, to);
          }}
        >
          <span>
            <label htmlFor="st-from">この日から</label>
            <input id="st-from" type="date" value={from} onChange={(e) => setFrom(e.target.value)} />
          </span>
          <span>
            <label htmlFor="st-to">この日まで</label>
            <input id="st-to" type="date" value={to} onChange={(e) => setTo(e.target.value)} />
          </span>
          <button type="submit" className="secondary">集計</button>
        </form>
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      {stats && (
        <>
          <section className="card" aria-labelledby="sum-h">
            <h2 id="sum-h">合計</h2>
            <p className="big amount">{formatYen(stats.total_minor)}</p>
            <p className="muted">支出 {stats.expense_count}件</p>
            {stats.unconfirmed_transfer_count > 0 && (
              <p className="notice warn">
                送金申告済み・受取確認待ち {stats.unconfirmed_transfer_count}件({formatYen(stats.unconfirmed_transfer_minor)})
              </p>
            )}
          </section>

          <section className="card" aria-labelledby="member-h">
            <h2 id="member-h">メンバー別</h2>
            <table aria-label="メンバー別の集計">
              <thead>
                <tr>
                  <th scope="col">メンバー</th>
                  <th scope="col">立て替えた額</th>
                  <th scope="col">負担する額</th>
                </tr>
              </thead>
              <tbody>
                {stats.by_member.map((m) => (
                  <tr key={m.member_id}>
                    <th scope="row">{names[m.member_id]}</th>
                    <td className="amount">{formatYen(m.paid_minor)}</td>
                    <td className="amount">{formatYen(m.share_minor)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </section>

          <section className="card" aria-labelledby="cat-h">
            <h2 id="cat-h">カテゴリ別</h2>
            {stats.by_category.length === 0 ? (
              <p className="muted">この期間の支出はありません</p>
            ) : (
              <ul className="list" aria-label="カテゴリ別の集計">
                {stats.by_category.map((c) => (
                  <li key={c.category ?? "none"}>
                    <span>
                      {categoryLabel(c.category)} <span className="muted">{c.count}件</span>
                    </span>
                    <span className="amount">{formatYen(c.total_minor)}</span>
                  </li>
                ))}
              </ul>
            )}
          </section>

          <section className="card" aria-labelledby="month-h">
            <h2 id="month-h">月別</h2>
            <ul className="list" aria-label="月別の集計">
              {stats.by_month.map((m) => (
                <li key={m.month}>
                  <span>
                    {m.month.replace("-", "年")}月 <span className="muted">{m.count}件</span>
                  </span>
                  <span className="amount">{formatYen(m.total_minor)}</span>
                </li>
              ))}
            </ul>
          </section>

          <section className="card" aria-labelledby="notes-h">
            <h2 id="notes-h">集計について</h2>
            <ul>
              {stats.notes.map((n) => (
                <li key={n}>{n}</li>
              ))}
              <li>この集計はグループのメンバーへの表示のためだけに計算しています(広告や外部への提供には使いません)</li>
            </ul>
          </section>
        </>
      )}
    </>
  );
}
