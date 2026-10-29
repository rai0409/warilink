"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Plus, X } from "lucide-react";
import { Body, BottomBar, ErrorText, Muted, PrimaryButton, Screen, TextButton, TextField, TopBar } from "@/components/kit";
import { api, ensureGuest, errorMessage, type Group } from "@/lib/api";
import { loadDefaultName, saveDefaultName } from "@/lib/prefs";
import { uuid } from "@/lib/uuid";

export default function NewGroupPage() {
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [myName, setMyName] = useState("");
  const [members, setMembers] = useState<string[]>(["", ""]);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [key] = useState(() => uuid());

  // 保存した名前は、まだ何も入力していないときだけ入れる(読み込み中に入力した名前を消さない)
  useEffect(() => setMyName((v) => v || loadDefaultName()), []);

  const names = members.map((m) => m.trim()).filter(Boolean);
  const ready = title.trim() !== "" && myName.trim() !== "";

  async function create() {
    setBusy(true);
    setError(null);
    try {
      await ensureGuest();
      const g = await api<Group>("/groups", {
        method: "POST",
        idempotencyKey: key,
        body: { title: title.trim(), my_name: myName.trim(), other_member_names: names },
      });
      saveDefaultName(myName.trim());
      router.push(`/g/${g.public_id}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Screen>
      <TopBar title="グループを作る" close="/" />
      <Body>
        <TextField label="グループ名" value={title} onChange={setTitle} onClear={() => setTitle("")} maxLength={100}
          placeholder="週末の京都旅行" />
        <TextField label="あなたの名前" value={myName} onChange={setMyName} onClear={() => setMyName("")} maxLength={40} />
        <p className="mt-6 mb-2 text-[13px] text-muted-foreground" id="members-label">メンバー</p>
        <ul aria-labelledby="members-label" className="space-y-2">
          {members.map((m, i) => (
            <li key={i} className="relative">
              <input
                aria-label={`メンバー${i + 1}`}
                value={m}
                maxLength={40}
                onChange={(e) => setMembers(members.map((x, j) => (j === i ? e.target.value : x)))}
                className="h-12 w-full rounded-md border border-input px-4 pr-11 text-[15px] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30"
              />
              <button
                type="button"
                aria-label={`メンバー${i + 1}を削除`}
                onClick={() => setMembers(members.filter((_, j) => j !== i))}
                className="absolute top-1/2 right-1 grid size-10 -translate-y-1/2 place-items-center text-muted-foreground"
              >
                <X className="size-4" aria-hidden />
              </button>
            </li>
          ))}
        </ul>
        <div className="mt-2 border-y border-border">
          <TextButton onClick={() => setMembers([...members, ""])}>
            <Plus className="size-5" aria-hidden />
            メンバーを追加
          </TextButton>
        </div>
        <Muted className="mt-3">名前だけでも追加できます</Muted>
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      <BottomBar>
        <PrimaryButton disabled={!ready || busy} onClick={create}>作成する</PrimaryButton>
      </BottomBar>
    </Screen>
  );
}
