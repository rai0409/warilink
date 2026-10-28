"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { InviteBox } from "@/components/InviteBox";
import { api, errorMessage } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function SettingsPage() {
  const { group, reload } = useGroup();
  const router = useRouter();
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [message, setMessage] = useState<string | null>(null);

  async function leave() {
    const note = group.is_owner
      ? others.length > 0
        ? "作成者は、参加の早いメンバーに自動で引き継がれます。"
        : "ほかに参加者がいないため、30日後にグループは完全に削除されます。"
      : "";
    if (!confirm(`このグループから抜けると、あなたはこのグループを見られなくなります(名前と記録は残ります)。${note}抜けますか?`)) return;
    try {
      await api(`/groups/${group.public_id}/leave`, { method: "POST" });
      router.push("/");
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  const [newOwner, setNewOwner] = useState("");
  const others = group.members.filter((m) => m.claimed && !m.is_me);

  async function transferOwner() {
    const target = others.find((m) => m.id === newOwner);
    if (!target || !confirm(`作成者を ${target.display_name} に引き継ぎます。よろしいですか?`)) return;
    try {
      await api(`/groups/${group.public_id}/owner`, { method: "POST", body: { member_id: target.id } });
      setError(null);
      setMessage(`作成者を ${target.display_name} に引き継ぎました`);
      await reload();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function closeGroup() {
    if (!confirm("グループを終了すると、以後は閲覧だけになり、招待URLもすべて使えなくなります。1年後に完全に削除します。終了しますか?")) return;
    try {
      await api(`/groups/${group.public_id}/close`, { method: "POST" });
      setError(null);
      setMessage("グループを終了しました(閲覧のみ)");
      await reload();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function revokeInvites() {
    if (!confirm("発行済みの招待URLをすべて使えなくします。参加済みのメンバーはそのままです。よろしいですか?")) return;
    try {
      const r = await api<{ revoked: number }>(`/groups/${group.public_id}/invites/revoke`, { method: "POST" });
      setError(null);
      setMessage(`招待URLを ${r.revoked} 件無効にしました`);
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  async function addMember(e: React.FormEvent) {
    e.preventDefault();
    if (!newName.trim()) return;
    try {
      await api(`/groups/${group.public_id}/members`, { method: "POST", body: { names: [newName.trim()] } });
      setNewName("");
      setError(null);
      await reload();
    } catch (err) {
      setError(errorMessage(err));
    }
  }

  return (
    <>
      <section className="card" aria-labelledby="mem-h">
        <h2 id="mem-h">メンバー</h2>
        <ul className="list">
          {group.members.map((m) => (
            <li key={m.id}>
              <span>{m.display_name}{m.is_me && "(あなた)"}</span>
              <span className="muted">{m.claimed ? "参加済み" : "未参加"}</span>
            </li>
          ))}
        </ul>
        <form onSubmit={addMember}>
          <label htmlFor="new-member">メンバーを追加</label>
          <div className="row">
            <input id="new-member" value={newName} maxLength={40} onChange={(e) => setNewName(e.target.value)} />
            <button type="submit" className="secondary" style={{ flex: "0 0 auto" }}>追加</button>
          </div>
        </form>
        <p className="muted">名前だけのメンバー(アプリを使わない人)は、参加者なら誰でも追加できます。</p>
        {group.is_owner ? (
          <>
            <InviteBox publicId={group.public_id} />
            <p className="row">
              <button type="button" className="secondary" onClick={revokeInvites}>招待URLをすべて無効にする</button>
            </p>
          </>
        ) : (
          <p className="muted">招待URLはグループを作成した人が発行できます。</p>
        )}
        {message && <p className="notice" role="status">{message}</p>}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      {group.is_owner && (
        <section className="card" aria-labelledby="owner-h">
          <h2 id="owner-h">作成者の引き継ぎ・グループの終了</h2>
          {others.length > 0 ? (
            <div className="row">
              <select aria-label="引き継ぐ相手" value={newOwner} onChange={(e) => setNewOwner(e.target.value)}>
                <option value="">引き継ぐ相手を選ぶ</option>
                {others.map((m) => (
                  <option key={m.id} value={m.id}>{m.display_name}</option>
                ))}
              </select>
              <button type="button" className="secondary" disabled={!newOwner} onClick={transferOwner}>
                作成者を引き継ぐ
              </button>
            </div>
          ) : (
            <p className="muted">引き継げる参加者がまだいません。</p>
          )}
          {!group.closed && (
            <p className="row">
              <button type="button" className="secondary" onClick={closeGroup}>グループを終了する</button>
            </p>
          )}
          <p className="muted">終了したグループは閲覧だけになり、1年後に完全に削除します。</p>
        </section>
      )}

      <section className="card" aria-labelledby="leave-h">
        <h2 id="leave-h">グループから抜ける</h2>
        <p className="muted">
          抜けた後もあなたの名前と支出・精算の記録は残り、ほかのメンバーは引き続き使えます。
          {group.is_owner && " 作成者が抜けると、参加の早いメンバーに自動で引き継がれます。"}
        </p>
        <p className="row">
          <button type="button" className="secondary" onClick={leave}>このグループから抜ける</button>
        </p>
      </section>

      <section className="card" aria-labelledby="recv-h">
        <h2 id="recv-h">受取設定(振込先)とアカウント</h2>
        <p>
          振込先は Passkey を登録した方だけが保存でき、あなたに支払う精算の相手にだけ表示します。{" "}
          <Link href="/account">アカウントと Passkey・振込先の設定へ</Link>
        </p>
        <p className="muted">
          ゲストのままでも割り勘と精算はすべて利用できます。ゲストの情報はこのブラウザの Cookie に紐づいています。Cookie
          を消すと、このグループに戻れなくなる場合があります。
        </p>
      </section>

      <section className="card" aria-labelledby="privacy-h">
        <h2 id="privacy-h">データの利用について</h2>
        <p><span className="badge">準備中</span> 次の利用は、それぞれ個別に同意した場合だけ行う予定です。現時点ではいずれも行っていません。</p>
        <ul>
          <li>OCR の精度改善(同意したレシート画像と訂正内容のみ)</li>
          <li>購買履歴を使った広告</li>
          <li>個人を特定しない統計分析</li>
        </ul>
        <p className="muted">銀行の明細を広告や販売に使うことはありません。</p>
        <p><Link href="/pricing">料金プランを見る</Link></p>
      </section>
    </>
  );
}
