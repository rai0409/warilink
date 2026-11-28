"use client";

import { useRouter } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import { Body, BottomBar, ErrorText, Muted, PrimaryButton, RadioRow, Screen, TextField, TopBar } from "@/components/kit";
import { api, ensureGuest, errorMessage, type Group, type Member } from "@/lib/api";
import { inviteToken } from "@/lib/invite";
import { loadDefaultName, saveDefaultName } from "@/lib/prefs";

type Preview = { group_title: string; unclaimed_members: Member[] };
type InvitePreview = { data: Preview; token: string; generation: number };
const NEW = "__new__";

export default function JoinPage() {
  const token = useRef<string | null>(null);
  const generation = useRef(0);
  const mounted = useRef(false);
  const joining = useRef(false);
  const [link, setLink] = useState("");
  const [loading, setLoading] = useState(false);
  const router = useRouter();
  const [preview, setPreview] = useState<InvitePreview | null>(null);
  const [choice, setChoice] = useState<string>("");
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const previewToken = useCallback(async (value: string) => {
    const current = ++generation.current;
    token.current = value;
    setLink("");
    setPreview(null);
    setChoice("");
    setNewName("");
    setError(null);
    setLoading(true);
    try {
      await ensureGuest();
      if (!mounted.current || generation.current !== current) return;
      const p = await api<Preview>("/invites/preview", { method: "POST", body: { token: value } });
      if (!mounted.current || generation.current !== current) return;
      setPreview({ data: p, token: value, generation: current });
      setNewName(loadDefaultName());
    } catch (e) {
      if (mounted.current && generation.current === current) setError(errorMessage(e));
    } finally {
      if (mounted.current && generation.current === current) setLoading(false);
    }
  }, []);

  useEffect(() => {
    mounted.current = true;
    const readFragment = () => {
      if (!window.location.hash) return;
      const value = inviteToken(window.location.href);
      history.replaceState(history.state, "", window.location.pathname + window.location.search);
      if (value) void previewToken(value);
    };
    window.addEventListener("hashchange", readFragment);
    readFragment();
    return () => {
      mounted.current = false;
      window.removeEventListener("hashchange", readFragment);
    };
  }, [previewToken]);

  function next() {
    const value = inviteToken(link);
    if (!value) {
      setError("招待リンクの形式が正しくありません。届いたリンクをそのまま貼り付けてください");
      return;
    }
    void previewToken(value);
  }

  const chosen = preview?.data.unclaimed_members.find((m) => m.id === choice);
  const label = choice === NEW ? newName.trim() : chosen?.display_name;

  async function join() {
    if (!preview || joining.current || preview.generation !== generation.current || preview.token !== token.current) return;
    const { token: value, generation: current } = preview;
    joining.current = true;
    setBusy(true);
    setError(null);
    try {
      const body = choice === NEW ? { token: value, new_member_name: newName.trim() } : { token: value, claim_member_id: choice };
      const g = await api<Group>("/invites/join", { method: "POST", body });
      if (!mounted.current || generation.current !== current) return;
      token.current = null;
      if (label) saveDefaultName(label);
      router.push(`/g/${g.public_id}`);
    } catch (e) {
      if (mounted.current && generation.current === current) setError(errorMessage(e));
    } finally {
      joining.current = false;
      if (mounted.current) setBusy(false);
    }
  }

  return (
    <Screen>
      <TopBar title="グループに参加" back="/" />
      <Body>
        {!preview ? (
          loading ? <Muted className="mt-6">読み込み中…</Muted> : <>
            <TextField label="招待リンク" value={link} onChange={setLink} onClear={() => setLink("")}
              placeholder="招待リンクまたはコード" inputMode="url" />
            {error && <ErrorText>{error}</ErrorText>}
            {token.current && error && <button type="button" onClick={() => void previewToken(token.current!)}>再試行</button>}
            <PrimaryButton onClick={next}>次へ</PrimaryButton>
          </>
        ) : (
          <>
            <p className="mt-6 text-[13px] text-muted-foreground">グループ名</p>
            <p className="mt-1 border-b border-border pb-5 text-[16px] font-semibold">{preview.data.group_title}</p>
            <fieldset className="mt-6">
              <legend className="mb-1 text-[13px] text-muted-foreground">あなたの名前</legend>
              <ul>
                {preview.data.unclaimed_members.map((m) => (
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
