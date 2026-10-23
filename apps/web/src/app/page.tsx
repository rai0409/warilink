"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api, ensureGuest, errorMessage, type Group } from "@/lib/api";

export default function StartPage() {
  const router = useRouter();
  const [title, setTitle] = useState("");
  const [myName, setMyName] = useState("");
  const [others, setOthers] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [key] = useState(() => crypto.randomUUID());

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await ensureGuest();
      const names = others.split(/[\n,、]/).map((s) => s.trim()).filter(Boolean);
      const g = await api<Group>("/groups", {
        method: "POST",
        idempotencyKey: key,
        body: { title, my_name: myName, other_member_names: names },
      });
      router.push(`/g/${g.public_id}`);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  }

  return (
    <>
      <h1>登録せずに割り勘を始める</h1>
      <p className="muted">メールアドレスや電話番号は不要です。作成後に招待URLで参加してもらえます。</p>
      <form className="card" onSubmit={submit}>
        <label htmlFor="title">グループ名</label>
        <input id="title" value={title} onChange={(e) => setTitle(e.target.value)} required maxLength={100} />
        <label htmlFor="me">あなたの名前</label>
        <input id="me" value={myName} onChange={(e) => setMyName(e.target.value)} required maxLength={40} />
        <label htmlFor="others">ほかのメンバー(カンマまたは改行区切り)</label>
        <input id="others" value={others} onChange={(e) => setOthers(e.target.value)} placeholder="Bさん, Cさん" />
        <p className="row" style={{ marginTop: 16 }}>
          <button type="submit" disabled={busy}>グループを作成</button>
        </p>
        {error && <p className="error" role="alert">{error}</p>}
      </form>
    </>
  );
}
