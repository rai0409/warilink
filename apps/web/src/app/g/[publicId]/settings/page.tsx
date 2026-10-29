"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { DoorOpen, Link2, LogOut, Pencil, UserRoundCog, UsersRound } from "lucide-react";
import { Body, ConfirmDialog, ErrorText, List, Row, Screen, SectionTitle, StatusText, TopBar } from "@/components/kit";
import { api, errorMessage, type InviteStatus } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function GroupSettingsPage() {
  const { group, reload } = useGroup();
  const router = useRouter();
  const pid = group.public_id;
  const [invite, setInvite] = useState<InviteStatus | null>(null);
  const [dialog, setDialog] = useState<"close" | "leave" | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const others = group.members.filter((m) => m.claimed && !m.is_me);

  useEffect(() => {
    api<InviteStatus>(`/groups/${pid}/invites/status`).then(setInvite).catch(() => setInvite(null));
  }, [pid]);

  async function closeGroup() {
    try {
      await api(`/groups/${pid}/close`, { method: "POST" });
      setDialog(null);
      setMessage("グループを終了しました(閲覧のみ)");
      await reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function leave() {
    try {
      await api(`/groups/${pid}/leave`, { method: "POST" });
      router.push("/");
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  const leaveNote = group.is_owner
    ? others.length > 0
      ? "作成者は、参加の早いメンバーに自動で引き継がれます。名前と記録は残ります。"
      : "ほかに参加者がいないため、30日後にグループは削除されます。"
    : "あなたはこのグループを見られなくなります。名前と記録は残ります。";

  return (
    <Screen>
      <TopBar title="グループ設定" subtitle={group.title} back={`/g/${pid}`} />
      <Body>
        <List className="mt-4">
          <Row icon={<Pencil strokeWidth={1.6} />} label="グループ名" value={group.title} chevron={false} />
          <Row href={`/g/${pid}/members`} icon={<UsersRound strokeWidth={1.6} />} label="メンバー" value={`${group.members.length}人`} />
          {group.is_owner && (
            <Row href={`/g/${pid}/invite`} icon={<Link2 strokeWidth={1.6} />} label="招待リンク" value={invite?.active ? "有効" : "無効"} />
          )}
        </List>
        <SectionTitle>管理</SectionTitle>
        <List>
          {group.is_owner && (
            <>
              <Row href={`/g/${pid}/settings/owner`} icon={<UserRoundCog strokeWidth={1.6} />} label="作成者を引き継ぐ" />
              {!group.closed && (
                <Row onClick={() => setDialog("close")} icon={<DoorOpen strokeWidth={1.6} />} label="グループを終了する" sub="終了後は閲覧のみ" />
              )}
            </>
          )}
          <Row onClick={() => setDialog("leave")} icon={<LogOut strokeWidth={1.6} />} label="グループから抜ける" tone="danger" />
        </List>
        {group.closed && <StatusText className="text-muted-foreground">このグループは終了しています。閲覧だけできます。</StatusText>}
        {message && <StatusText>{message}</StatusText>}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      <ConfirmDialog open={dialog === "close"} onOpenChange={(v) => !v && setDialog(null)} title="グループを終了しますか?"
        description="以後は閲覧だけになり、招待リンクも使えなくなります。1年後に削除されます。" confirmLabel="終了する" danger
        onConfirm={closeGroup} />
      <ConfirmDialog open={dialog === "leave"} onOpenChange={(v) => !v && setDialog(null)} title="グループから抜けますか?"
        description={leaveNote} confirmLabel="抜ける" danger onConfirm={leave} />
    </Screen>
  );
}
