"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Body, ConfirmDialog, ErrorText, Muted, RadioRow, Screen, TopBar } from "@/components/kit";
import { api, errorMessage } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

/** 作成者の引き継ぎ。参加済みのメンバーだけを選べる(名前だけのメンバーには渡せない) */
export default function OwnerTransferPage() {
  const { group, reload } = useGroup();
  const router = useRouter();
  const pid = group.public_id;
  const [target, setTarget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const others = group.members.filter((m) => !m.is_me);
  const chosen = others.find((m) => m.id === target);

  async function transfer() {
    try {
      await api(`/groups/${pid}/owner`, { method: "POST", body: { member_id: target } });
      await reload();
      router.push(`/g/${pid}/settings`);
    } catch (e) {
      setError(errorMessage(e));
      setTarget(null);
    }
  }

  return (
    <Screen>
      <TopBar title="作成者の引き継ぎ" back={`/g/${pid}/settings`} />
      <Body>
        {!group.is_owner ? (
          <Muted className="mt-6">引き継げるのはグループを作成した人だけです。</Muted>
        ) : (
          <fieldset className="mt-4">
            <legend className="mb-2 text-[13px] text-muted-foreground">次の作成者を選んでください。</legend>
            <ul>
              {others.map((m) => (
                <RadioRow key={m.id} name="owner" checked={target === m.id} onChange={() => setTarget(m.id)}
                  label={m.display_name} disabled={!m.claimed}
                  right={!m.claimed ? <span className="text-[12px] text-muted-foreground">名前のみ</span> : undefined} />
              ))}
            </ul>
          </fieldset>
        )}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      <ConfirmDialog open={chosen !== undefined} onOpenChange={(v) => !v && setTarget(null)}
        title={`${chosen?.display_name ?? ""}に引き継ぎますか?`} description="招待リンクやグループの管理を引き継ぎます。"
        confirmLabel="引き継ぐ" onConfirm={transfer} />
    </Screen>
  );
}
