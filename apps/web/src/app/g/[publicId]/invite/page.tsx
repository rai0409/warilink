"use client";

import { useCallback, useEffect, useState } from "react";
import { Copy, Share } from "lucide-react";
import { Body, BottomBar, ConfirmDialog, ErrorText, Muted, PrimaryButton, Screen, StatusText, TextButton, TopBar } from "@/components/kit";
import { api, errorMessage, type InviteStatus } from "@/lib/api";
import { useGroup } from "@/lib/group-context";
import { loadInviteUrl, saveInviteUrl } from "@/lib/prefs";

/** 招待リンク(作成者だけ)。サーバーはリンクのハッシュしか持たないため、URL はこの端末に覚えておく */
export default function InvitePage() {
  const { group } = useGroup();
  const pid = group.public_id;
  const [status, setStatus] = useState<InviteStatus | null>(null);
  const [url, setUrl] = useState<string | null>(null);
  const [confirm, setConfirm] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    try {
      const st = await api<InviteStatus>(`/groups/${pid}/invites/status`);
      setStatus(st);
      setUrl(st.active ? loadInviteUrl(pid) : null);
      if (!st.active) saveInviteUrl(pid, null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [pid]);

  useEffect(() => {
    load();
  }, [load]);

  async function create() {
    setError(null);
    try {
      const inv = await api<{ token: string }>(`/groups/${pid}/invites`, { method: "POST", body: {} });
      const u = `${location.origin}/join/${inv.token}`;
      saveInviteUrl(pid, u);
      setUrl(u);
      setStatus({ active: true, expires_at: null });
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  async function share() {
    if (!url) return;
    try {
      if (navigator.share) await navigator.share({ title: group.title, url });
      else {
        await navigator.clipboard.writeText(url);
        setMessage("リンクをコピーしました");
      }
    } catch {
      /* 共有を取り消した場合は何もしない */
    }
  }

  async function copy() {
    if (!url) return;
    try {
      await navigator.clipboard.writeText(url);
      setMessage("リンクをコピーしました");
    } catch {
      setError("コピーできませんでした。リンクを長押ししてコピーしてください");
    }
  }

  async function revoke() {
    try {
      await api(`/groups/${pid}/invites/revoke`, { method: "POST" });
      saveInviteUrl(pid, null);
      setConfirm(false);
      setMessage("リンクを無効にしました");
      await load();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <Screen>
      <TopBar title="招待リンク" subtitle={group.title} back={`/g/${pid}`} />
      <Body>
        {!group.is_owner ? (
          <Muted className="mt-6">招待リンクはグループを作成した人が発行できます。</Muted>
        ) : (
          status && (
            <>
              <p className="mt-6 flex items-center gap-2 text-[14px]">
                <span aria-hidden className={`size-2.5 rounded-full ${status.active ? "bg-success" : "bg-[#c9cfd8]"}`} />
                {status.active ? "有効" : "無効"}
              </p>
              {url ? (
                <>
                  <div className="mt-4 flex items-center gap-2 rounded-lg bg-muted px-4">
                    <input
                      aria-label="招待URL"
                      readOnly
                      value={url}
                      onFocus={(e) => e.currentTarget.select()}
                      className="h-12 min-w-0 flex-1 bg-transparent text-[14px] outline-none"
                    />
                    <button type="button" aria-label="リンクをコピー" onClick={copy} className="grid size-10 place-items-center text-primary">
                      <Copy className="size-5" aria-hidden />
                    </button>
                  </div>
                  <PrimaryButton className="mt-3" onClick={share}>
                    <Share className="size-5" aria-hidden />
                    リンクを共有
                  </PrimaryButton>
                </>
              ) : (
                <>
                  {status.active && <Muted className="mt-3">この端末では以前のリンクを表示できません。新しいリンクを作れます。</Muted>}
                  <PrimaryButton className="mt-4" onClick={create} disabled={group.closed}>招待URLを作成</PrimaryButton>
                </>
              )}
              <Muted className="mt-6">リンクから名前を選んで参加できます。</Muted>
            </>
          )
        )}
        {message && <StatusText>{message}</StatusText>}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      {group.is_owner && status?.active && (
        <BottomBar className="border-t border-border text-center">
          <TextButton tone="danger" className="w-full justify-center" onClick={() => setConfirm(true)}>
            リンクを無効にする
          </TextButton>
        </BottomBar>
      )}
      <ConfirmDialog open={confirm} onOpenChange={setConfirm} title="リンクを無効にしますか?"
        description="発行済みの招待リンクがすべて使えなくなります。参加済みのメンバーはそのままです。"
        confirmLabel="無効にする" danger onConfirm={revoke} />
    </Screen>
  );
}
