"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { Body, BottomBar, ErrorText, Muted, PrimaryButton, Screen, TextField, TopBar } from "@/components/kit";

/** 招待リンク(またはその末尾のコード)を貼り付けて参加画面へ進む */
export default function JoinByLinkPage() {
  const router = useRouter();
  const [link, setLink] = useState("");
  const [error, setError] = useState<string | null>(null);

  function next() {
    const m = link.trim().match(/(?:\/join\/)?([A-Za-z0-9_-]{16,})\/?$/);
    if (!m) {
      setError("招待リンクの形式が正しくありません。届いたリンクをそのまま貼り付けてください");
      return;
    }
    router.push(`/join/${m[1]}`);
  }

  return (
    <Screen>
      <TopBar title="招待リンクから参加" back="/" />
      <Body>
        <TextField label="招待リンク" value={link} onChange={setLink} onClear={() => setLink("")}
          placeholder="https://…/join/…" inputMode="url" />
        <Muted className="mt-3">グループの作成者から届いたリンクを貼り付けてください。</Muted>
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      <BottomBar>
        <PrimaryButton disabled={!link.trim()} onClick={next}>次へ</PrimaryButton>
      </BottomBar>
    </Screen>
  );
}
