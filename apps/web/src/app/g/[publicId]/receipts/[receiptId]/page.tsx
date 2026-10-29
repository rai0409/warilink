"use client";

import { useParams, useRouter } from "next/navigation";
import { useEffect, useMemo, useState } from "react";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { Body, ButtonLink, ErrorText, Muted, Screen, TopBar } from "@/components/kit";
import { cn } from "@/lib/utils";
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

  const back = `/g/${group.public_id}/receipt`;
  const simple = (title: string, children: React.ReactNode) => (
    <Screen>
      <TopBar title={title} back={back} />
      <Body>{children}</Body>
    </Screen>
  );
  if (!receipt) return simple("読み取り結果", error ? <ErrorText>{error}</ErrorText> : <Muted className="mt-6">読み込み中…</Muted>);

  if (PENDING.has(receipt.state)) {
    return simple("読み取り中です", (
      <div aria-live="polite" className="mt-6">
        <p className="text-[14px]">
          {receipt.state === "uploaded" ? "順番を待っています…" : "文字を読み取っています…"}
          {receipt.attempts > 1 && `(再試行 ${receipt.attempts}回目)`}
        </p>
        <Muted className="mt-2">1枚あたり数秒〜数十秒かかります。この画面を閉じても読み取りは続きます。</Muted>
      </div>
    ));
  }
  if (receipt.state === "failed") {
    return simple("読み取れませんでした", (
      <>
        <p className="mt-6 text-[14px] text-destructive">{receipt.failure_message}</p>
        <Muted className="mt-2">読み取り枠は消費されていません(返還済み)。</Muted>
        <ButtonLink href={back} className="mt-6">手入力で登録する</ButtonLink>
      </>
    ));
  }
  if (receipt.state === "confirmed" && receipt.expense_id) {
    return simple("このレシートは登録済みです", (
      <ButtonLink href={`/g/${group.public_id}/expenses/${receipt.expense_id}`} variant="outline" className="mt-6">登録した支出を見る</ButtonLink>
    ));
  }
  if (!draft || !editor) return simple("読み取り結果", <ErrorText>読み取り結果がありません</ErrorText>);

  const scale = draft.image_scale || 1;
  const box = (b: number[]) => ({
    left: `${(b[0] / scale / receipt.width) * 100}%`,
    top: `${(b[1] / scale / receipt.height) * 100}%`,
    width: `${((b[2] - b[0]) / scale / receipt.width) * 100}%`,
    height: `${((b[3] - b[1]) / scale / receipt.height) * 100}%`,
  });
  const lines = [...draft.items, ...draft.adjustments];

  return (
    <ReceiptEditor
      publicId={group.public_id}
      members={group.members}
      myMemberId={group.my_member_id}
      initialDraft={editor.initial}
      review={editor.review}
      sourceReceiptId={receipt.id}
      onFocusLine={setFocused}
      backHref={back}
      onSaved={(id) => router.push(`/g/${group.public_id}/expenses/${id}`)}
      header={
        <section aria-label="読み取り結果の確認" className="mt-4">
          <p className="text-[13px] text-warning">
            読み取った内容は下書きです。画像と見比べて、商品名・数量・金額・合計を確認してください。
            {draft.items.some((i) => i.needs_review) && " 「要確認」の行は特に注意してください。"}
          </p>
          {draft.warnings.filter((w) => w.code !== "TOTAL_MISMATCH" && w.code !== "ITEMS_NEED_REVIEW").map((w) => (
            <Muted key={w.code}>・{w.message}</Muted>
          ))}
          <div className="relative mx-auto mt-3 max-w-[360px] overflow-hidden rounded-lg border border-border">
            {/* eslint-disable-next-line @next/next/no-img-element -- 認証付き API の画像(最適化不要) */}
            <img src={receipt.image_url} alt="アップロードしたレシート" className="block h-auto w-full" />
            {lines.map((l) => (
              <span key={l.id} aria-hidden="true" style={box(l.bbox)}
                className={cn("pointer-events-none absolute rounded-sm border-2 border-primary/60",
                  l.needs_review && "border-warning bg-warning/10", focused === l.id && "border-[3px] border-foreground bg-yellow-300/30")} />
            ))}
          </div>
          {receipt.run && (
            <Muted className="mt-1">
              読み取り: {String(receipt.run.model_name)}(約{Math.round(Number(receipt.run.elapsed_ms) / 100) / 10}秒)
            </Muted>
          )}
        </section>
      }
    />
  );
}
