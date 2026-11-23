"use client";

import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Body, ErrorText, Muted, PrimaryButton, Screen, TopBar } from "@/components/kit";
import { api, errorMessage, type Group } from "@/lib/api";
import { GroupContext } from "@/lib/group-context";

/** /g/[publicId] 配下の画面が使うグループ情報を読み込む(各画面が自分の画面名と主ボタンを持つ) */
export default function GroupLayout({ children }: { children: React.ReactNode }) {
  const { publicId } = useParams<{ publicId: string }>();
  return <GroupLoader key={publicId} publicId={publicId}>{children}</GroupLoader>;
}

function GroupLoader({ publicId, children }: { publicId: string; children: React.ReactNode }) {
  const [group, setGroup] = useState<Group | null>(null);
  const [error, setError] = useState<string | null>(null);
  const generation = useRef(0);

  const reload = useCallback(async () => {
    const current = ++generation.current;
    try {
      const result = await api<Group>(`/groups/${publicId}`);
      if (current !== generation.current) return;
      if (result.public_id !== publicId) throw new Error("group ID mismatch");
      setGroup(result);
      setError(null);
    } catch (e) {
      if (current !== generation.current) return;
      setGroup(null);
      setError(errorMessage(e));
    }
  }, [publicId]);

  useEffect(() => {
    void reload();
    return () => { generation.current++; };
  }, [reload]);

  if (!group)
    return (
      <Screen>
        <TopBar title="グループ" back="/" />
        <Body>{error ? <><ErrorText>{error}</ErrorText><PrimaryButton className="mt-4" onClick={() => void reload()}>再試行</PrimaryButton></> : <Muted className="mt-6">読み込み中…</Muted>}</Body>
      </Screen>
    );
  return <GroupContext.Provider value={{ group, reload }}>{children}</GroupContext.Provider>;
}
