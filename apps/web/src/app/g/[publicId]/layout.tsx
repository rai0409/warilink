"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { Body, ErrorText, Muted, Screen, TopBar } from "@/components/kit";
import { api, errorMessage, type Group } from "@/lib/api";
import { GroupContext } from "@/lib/group-context";

/** /g/[publicId] 配下の画面が使うグループ情報を読み込む(各画面が自分の画面名と主ボタンを持つ) */
export default function GroupLayout({ children }: { children: React.ReactNode }) {
  const { publicId } = useParams<{ publicId: string }>();
  const [group, setGroup] = useState<Group | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setGroup(await api<Group>(`/groups/${publicId}`));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [publicId]);

  useEffect(() => {
    reload();
  }, [reload]);

  if (!group)
    return (
      <Screen>
        <TopBar title="グループ" back="/" />
        <Body>{error ? <ErrorText>{error}</ErrorText> : <Muted className="mt-6">読み込み中…</Muted>}</Body>
      </Screen>
    );
  return <GroupContext.Provider value={{ group, reload }}>{children}</GroupContext.Provider>;
}
