"use client";

import Link from "next/link";
import { useState } from "react";
import { InviteBox } from "@/components/InviteBox";
import { api, errorMessage } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function SettingsPage() {
  const { group, reload } = useGroup();
  const [newName, setNewName] = useState("");
  const [error, setError] = useState<string | null>(null);

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
        {group.is_owner && <InviteBox publicId={group.public_id} />}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="card" aria-labelledby="recv-h">
        <h2 id="recv-h">受取設定(振込先)</h2>
        <p><span className="badge">準備中</span> 振込先の保存は、Passkey で登録した方だけが使える機能として準備中です。</p>
        <p className="muted">
          登録後は、振込先を精算の当事者(あなたに支払う人)にだけ表示します。現在は振込先を保存できません。
          ゲストのままでも割り勘と精算はすべて利用できます。
        </p>
        <p className="muted">
          ゲストの情報はこのブラウザの Cookie に紐づいています。Cookie を消すと、このグループに戻れなくなる場合があります。
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
