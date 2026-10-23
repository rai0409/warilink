"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { api, errorMessage, type Quota, type Receipt } from "@/lib/api";

export function ReceiptUpload({ publicId, available }: { publicId: string; available: boolean }) {
  const router = useRouter();
  const [quota, setQuota] = useState<Quota | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Quota>("/ocr/quota").then(setQuota).catch(() => setQuota(null));
  }, []);

  async function onFile(file: File | undefined) {
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const r = await api<Receipt>(`/groups/${publicId}/receipts`, { method: "POST", form });
      router.push(`/g/${publicId}/receipts/${r.id}`);
    } catch (e) {
      setError(errorMessage(e));
      setBusy(false);
    }
  }

  if (!available) {
    return (
      <p>
        <span className="badge">停止中</span> 現在、画像の読み取りを利用できません。下の欄にレシートの内容を手で入力してください。
      </p>
    );
  }
  return (
    <div>
      <p className="muted">
        読み取った内容は下書きです。必ず確認・訂正してから登録します(自動で確定しません)。
        {quota && ` 今月の残り: ${quota.remaining}枚(月${quota.free}枚まで)`}
      </p>
      <label className="button file-button">
        {busy ? "アップロード中…" : "レシートを撮影・画像を選ぶ"}
        <input
          type="file"
          accept="image/jpeg,image/png,image/webp"
          capture="environment"
          disabled={busy || quota?.remaining === 0}
          onChange={(e) => onFile(e.target.files?.[0])}
        />
      </label>
      {quota?.remaining === 0 && <p className="muted">今月の読み取り枠を使い切りました。手入力で登録できます。</p>}
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}
