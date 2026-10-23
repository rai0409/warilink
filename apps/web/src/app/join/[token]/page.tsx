"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, ensureGuest, errorMessage, type Group, type Member } from "@/lib/api";

type Preview = { group_title: string; unclaimed_members: Member[] };

export default function JoinPage() {
  const { token } = useParams<{ token: string }>();
  const router = useRouter();
  const [preview, setPreview] = useState<Preview | null>(null);
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    ensureGuest()
      .then(() => api<Preview>(`/invites/${encodeURIComponent(token)}`))
      .then(setPreview)
      .catch((e) => setError(errorMessage(e)));
  }, [token]);

  async function join(body: { claim_member_id?: string; new_member_name?: string }) {
    setError(null);
    try {
      const g = await api<Group>(`/invites/${encodeURIComponent(token)}/join`, { method: "POST", body });
      router.push(`/g/${g.public_id}`);
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  if (error && !preview) return <p className="error" role="alert">{error}</p>;
  if (!preview) return <p className="muted">読み込み中…</p>;
  return (
    <>
      <h1>「{preview.group_title}」に参加</h1>
      <section className="card">
        <h2>あなたはどの名前ですか?</h2>
        <div className="row">
          {preview.unclaimed_members.map((m) => (
            <button key={m.id} className="secondary" onClick={() => join({ claim_member_id: m.id })}>
              {m.display_name}
            </button>
          ))}
        </div>
        <label htmlFor="newname">一覧にない場合は名前を入力</label>
        <div className="row">
          <input id="newname" value={newName} onChange={(e) => setNewName(e.target.value)} maxLength={40} />
          <button disabled={!newName.trim()} onClick={() => join({ new_member_name: newName })}>参加</button>
        </div>
        {error && <p className="error" role="alert">{error}</p>}
      </section>
    </>
  );
}
