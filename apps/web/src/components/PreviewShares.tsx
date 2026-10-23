import type { Issue, Member } from "@/lib/api";
import { formatYen } from "@/lib/format";

export function PreviewShares({
  members,
  shares,
  label = "負担額のプレビュー",
}: {
  members: Member[];
  shares: Record<string, number>;
  label?: string;
}) {
  // 表示順はメンバー順で固定する(サーバー応答のキー順に依存しない)
  return (
    <ul className="list" aria-label={label}>
      {members
        .filter((m) => shares[m.id] !== undefined)
        .map((m) => (
          <li key={m.id}>
            <span>{m.display_name}</span>
            <span className="amount">{formatYen(shares[m.id])}</span>
          </li>
        ))}
    </ul>
  );
}

/** 対象を持たない(支出全体の)問題点だけを表示する。商品・調整行の問題は各行に出す。 */
export function IssueList({ issues, onlyGlobal = false }: { issues: Issue[]; onlyGlobal?: boolean }) {
  const shown = onlyGlobal ? issues.filter((i) => !i.target) : issues;
  if (shown.length === 0) return null;
  return (
    <ul className="issues" aria-label="確認が必要な点">
      {shown.map((i, n) => (
        <li key={n} className="error">{i.message}</li>
      ))}
    </ul>
  );
}
