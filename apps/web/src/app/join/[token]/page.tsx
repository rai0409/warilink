"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Body, BottomBar, ErrorText, Muted, PrimaryButton, RadioRow, Screen, TopBar } from "@/components/kit";
import { api, ensureGuest, errorMessage, type Group, type Member } from "@/lib/api";
import { loadDefaultName, saveDefaultName } from "@/lib/prefs";

type Preview = { group_title: string; unclaimed_members: Member[] };
const NEW = "__new__";

export default function JoinPage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [choice, setChoice] = useState<string>("");
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    ensureGuest()
      .then(() => api<Preview>(`/invites/${encodeURIComponent(token)}`))
      .then((p) => {
        setPreview(p);
        setNewName(loadDefaultName());
      })
      .catch((e) => setError(errorMessage(e)));
  }, [token]);

  const chosen = preview?.unclaimed_members.find((m) => m.id === choice);
  const label = choice === NEW ? newName.trim() : chosen?.display_name;

  async function join() {
    setBusy(true);
    setError(null);
    try {
      const body = choice === NEW ? { new_member_name: newName.trim() } : { claim_member_id: choice };
      const g = await api<Group>(`/invites/${encodeURIComponent(token)}/join`, { method: "POST", body });
      if (label) saveDefaultName(label);
      router.push(`/g/${g.public_id}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  return (
    <Screen>
      <TopBar title="グループに参加" back="/" />
      <Body>
        {!preview ? (
          error ? <ErrorText>{error}</ErrorText> : <Muted className="mt-6">読み込み中…</Muted>
        ) : (
          <>
            <p className="mt-6 text-[13px] text-muted-foreground">グループ名</p>
            <p className="mt-1 border-b border-border pb-5 text-[16px] font-semibold">{preview.group_title}</p>
            <fieldset className="mt-6">
              <legend className="mb-1 text-[13px] text-muted-foreground">あなたの名前</legend>
              <ul>
                {preview.unclaimed_members.map((m) => (
                  <RadioRow key={m.id} name="who" checked={choice === m.id} onChange={() => setChoice(m.id)}
                    label={m.display_name} />
                ))}
              </ul>
              <ul className="mt-6">
                <RadioRow name="who" checked={choice === NEW} onChange={() => setChoice(NEW)} label="新しい名前" />
              </ul>
              {choice === NEW && (
                <input
                  aria-label="新しい名前"
                  value={newName}
                  maxLength={40}
                  autoFocus
                  placeholder="あなたの名前を入力"
                  onChange={(e) => setNewName(e.target.value)}
                  className="mt-3 h-12 w-full rounded-md border border-input px-4 text-[15px] outline-none focus-visible:border-ring focus-visible:ring-[3px] focus-visible:ring-ring/30"
                />
              )}
            </fieldset>
            {error && <ErrorText>{error}</ErrorText>}
          </>
        )}
      </Body>
      {preview && (
        <BottomBar>
          <PrimaryButton disabled={!label || busy} onClick={join}>
            {label ? `${label}として参加` : "参加する"}
          </PrimaryButton>
        </BottomBar>
      )}
    </Screen>
  );
}
