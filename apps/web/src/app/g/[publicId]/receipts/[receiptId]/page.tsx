"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { api, errorMessage, type Receipt } from "@/lib/api";
import { useGroup } from "@/lib/group-context";
import { draftToEditor, type OcrDraft } from "@/lib/ocr-draft";

const PENDING = new Set(["uploaded", "processing"]);

export default function ReceiptReviewPage() {
  const { group } = useGroup();
  const { receiptId } = useParams<{ receiptId: string }>();
  const router = useRouter();
  const [receipt, setReceipt] = useState<Receipt | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [focused, setFocused] = useState<string | null>(null);
  const path = `/groups/${group.public_id}/receipts/${receiptId}`;

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    let stopped = false;
    const poll = async () => {
      try {
        const r = await api<Receipt>(path);
        if (stopped) return;
        setReceipt(r);
        setError(null);
        if (PENDING.has(r.state)) timer = setTimeout(poll, 1500);
      } catch (e) {
        if (!stopped) setError(errorMessage(e));
      }
    };
    poll();
    return () => {
      stopped = true;
      clearTimeout(timer);
    };
  }, [path]);

  const draft = receipt?.draft as unknown as OcrDraft | null | undefined;
  const editor = useMemo(() => (draft ? draftToEditor(draft) : null), [draft]);

  if (!receipt) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">読み込み中…</p>;

  if (PENDING.has(receipt.state)) {
    return (
      <section className="card" aria-live="polite" aria-labelledby="wait-h">
        <h2 id="wait-h">読み取り中です</h2>
        <p>
          {receipt.state === "uploaded" ? "順番を待っています…" : "文字を読み取っています…"}
          {receipt.attempts > 1 && `(再試行 ${receipt.attempts}回目)`}
        </p>
        <p className="muted">1枚あたり数秒〜数十秒かかります。この画面を閉じても読み取りは続きます。</p>
      </section>
    );
  }
  if (receipt.state === "failed") {
    return (
      <section className="card" aria-labelledby="fail-h">
        <h2 id="fail-h">読み取れませんでした</h2>
        <p className="error">{receipt.failure_message}</p>
        <p className="muted">読み取り枠は消費されていません(返還済み)。</p>
        <p><Link className="button" href={`/g/${group.public_id}/receipt`}>手入力で登録する</Link></p>
      </section>
    );
  }
  if (receipt.state === "confirmed" && receipt.expense_id) {
    return (
      <section className="card">
        <h2>このレシートは登録済みです</h2>
        <p><Link href={`/g/${group.public_id}/expenses/${receipt.expense_id}`}>登録した支出を見る</Link></p>
      </section>
    );
  }
  if (!draft || !editor) return <p className="error">読み取り結果がありません</p>;

  const scale = draft.image_scale || 1;
  const box = (b: number[]) => ({
    left: `${(b[0] / scale / receipt.width) * 100}%`,
    top: `${(b[1] / scale / receipt.height) * 100}%`,
    width: `${((b[2] - b[0]) / scale / receipt.width) * 100}%`,
    height: `${((b[3] - b[1]) / scale / receipt.height) * 100}%`,
  });
  const lines = [...draft.items, ...draft.adjustments];

  return (
    <>
      <section className="card" aria-labelledby="review-h">
        <h2 id="review-h">読み取り結果の確認</h2>
        <p className="notice warn">
          読み取った内容は下書きです。画像と見比べて、商品名・数量・金額・合計を確認してください。
          {draft.items.some((i) => i.needs_review) && " 「要確認」の行は特に注意してください。"}
        </p>
        {draft.warnings.filter((w) => w.code !== "TOTAL_MISMATCH" && w.code !== "ITEMS_NEED_REVIEW").map((w) => (
          <p key={w.code} className="muted">・{w.message}</p>
        ))}
        <div className="receipt-image">
          {/* eslint-disable-next-line @next/next/no-img-element -- 認証付き API の画像(最適化不要) */}
          <img src={receipt.image_url} alt="アップロードしたレシート" />
          {lines.map((l) => (
            <span
              key={l.id}
              className={`bbox${l.needs_review ? " low" : ""}${focused === l.id ? " focused" : ""}`}
              style={box(l.bbox)}
              aria-hidden="true"
            />
          ))}
        </div>
        {receipt.run && (
          <p className="muted small">
            読み取り: {String(receipt.run.model_name)}(約{Math.round(Number(receipt.run.elapsed_ms) / 100) / 10}秒)
          </p>
        )}
      </section>
      <ReceiptEditor
        publicId={group.public_id}
        members={group.members}
        myMemberId={group.my_member_id}
        initialDraft={editor.initial}
        review={editor.review}
        sourceReceiptId={receipt.id}
        onFocusLine={setFocused}
        onSaved={(id) => router.push(`/g/${group.public_id}/expenses/${id}`)}
      />
    </>
  );
}
