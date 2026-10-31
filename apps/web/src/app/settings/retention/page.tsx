"use client";

import { useEffect, useState } from "react";
import { Archive, FileText, Image as ImageIcon, KeyRound, Link2, UsersRound } from "lucide-react";
import { Body, ErrorText, List, Muted, Row, Screen, SectionTitle, TopBar } from "@/components/kit";
import { api, errorMessage, type Retention } from "@/lib/api";

type Key = Retention["policies"][number]["key"];

const ROWS: { key: Key; label: string; sub: string; icon: React.ReactNode }[] = [
  { key: "receipt_images", label: "画像", sub: "レシートの写真と読み取った文字", icon: <ImageIcon /> },
  { key: "audit_logs", label: "操作の記録", sub: "不正利用の調査のための記録", icon: <FileText /> },
  { key: "expired_sessions", label: "ログインの記録", sub: "失効・期限切れから", icon: <KeyRound /> },
  { key: "expired_invites", label: "招待リンク", sub: "失効・期限切れから", icon: <Link2 /> },
  { key: "orphaned_groups", label: "参加者がいなくなったグループ", sub: "最後の参加者が抜けてから", icon: <UsersRound /> },
  { key: "closed_groups", label: "終了したグループ", sub: "終了してから", icon: <UsersRound /> },
  { key: "backups", label: "バックアップ", sub: "削除する前のデータを含む", icon: <Archive /> },
];

function period(days: number): string {
  return days % 365 === 0 ? `${days / 365}年` : `${days}日`;
}

function day(iso: string): string {
  const d = new Date(iso);
  return `${d.getMonth() + 1}月${d.getDate()}日`;
}

/**
 * データの保存期間。方針(設定値)と、期限を過ぎたデータを消す処理が実際に動いているか(記録)を分けて示す。
 * 処理が動いていることを確認できない環境では「自動で削除します」と約束しない。
 */
export default function RetentionPage() {
  const [r, setR] = useState<Retention | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Retention>("/retention").then(setR).catch((e) => setError(errorMessage(e)));
  }, []);

  const days = new Map(r?.policies.map((p) => [p.key, p.days]));
  const ad = r?.automatic_deletion;

  return (
    <Screen>
      <TopBar title="データの保存期間" back="/settings" />
      <Body>
        {error && <ErrorText>{error}</ErrorText>}
        {r && (
          <>
            <List aria-label="データの保存期間" className="mt-4">
              {ROWS.filter((row) => days.has(row.key)).map((row) => (
                <Row key={row.key} icon={row.icon} label={row.label} sub={row.sub} chevron={false}
                  value={<span className="text-foreground">{period(days.get(row.key)!)}</span>} />
              ))}
            </List>
            <p role="status" className="mt-3 text-[13px] text-muted-foreground">
              {ad?.status === "verified" && ad.last_success_at
                ? `期間を過ぎたデータを消す処理が動いています(最終: ${day(ad.last_success_at)})。`
                : "この環境では、期間を過ぎたデータを消す処理が動いていることを確認できません。期間を過ぎても残っている場合があります。"}
            </p>
            <div className="mt-10 border-t border-border pt-2">
              <SectionTitle className="text-[14px] text-foreground">退会後に残る情報</SectionTitle>
              <ul className="list-disc space-y-1 pl-5 text-[13px] text-muted-foreground">
                <li>振込先は、退会と同時に元に戻せない形で消去します(伏せ字の表示だけ残ります)。</li>
                <li>参加していたグループには、ほかのメンバーとの記録として、名前と支出・精算の記録が残ります。</li>
                <li>
                  削除する前に取ったバックアップには、最長{period(days.get("backups") ?? 30)}残ります。
                  {r.backup_encrypted_offsite ? "" : "バックアップの暗号化と保管先は準備中です。"}
                  バックアップから戻した場合も、削除は再び適用します。
                </li>
              </ul>
            </div>
          </>
        )}
        {!r && !error && <Muted className="mt-6">読み込み中…</Muted>}
      </Body>
    </Screen>
  );
}
