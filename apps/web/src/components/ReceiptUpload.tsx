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

  if (!available) return null;
  const disabled = busy || quota?.remaining === 0;
  return (
    <div className="mt-4">
      <label
        className={`flex min-h-12 cursor-pointer items-center justify-center gap-2 rounded-lg border border-dashed border-primary/50 text-[14px] font-medium text-primary focus-within:ring-[3px] focus-within:ring-ring/30 ${disabled ? "cursor-not-allowed opacity-60" : ""}`}
      >
        {busy ? "アップロード中…" : "レシートを撮影・画像を選ぶ"}
        <input type="file" className="sr-only" accept="image/jpeg,image/png,image/webp" capture="environment"
          disabled={disabled} onChange={(e) => onFile(e.target.files?.[0])} />
      </label>
      <p className="mt-2 text-[12px] text-muted-foreground">
        読み取った内容は下書きです。必ず確認してから登録します。
        {quota && ` 今月の残り: ${quota.remaining}枚(月${quota.free}枚まで)`}
        画像は一定期間後に削除し、撮影位置などの情報(EXIF)は保存しません。
      </p>
      {quota?.remaining === 0 && <p className="text-[12px] text-muted-foreground">今月の読み取り枠を使い切りました。手入力で登録できます。</p>}
      {error && <p className="mt-2 text-[13px] text-destructive" role="alert">{error}</p>}
    </div>
  );
}
