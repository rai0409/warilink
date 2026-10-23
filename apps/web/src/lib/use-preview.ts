"use client";

import { useEffect, useState } from "react";
import { api, errorMessage, type ExpenseBody, type ExpensePreview } from "@/lib/api";

/**
 * 入力が変わるたびにサーバーで負担額を計算する(250ms デバウンス)。
 * 画面側では金額を計算しない。古い応答が新しい応答を上書きしないよう破棄する。
 */
export function useExpensePreview(publicId: string, body: ExpenseBody | null, expenseId: string) {
  const [preview, setPreview] = useState<ExpensePreview | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [pending, setPending] = useState(false);

  useEffect(() => {
    if (!body) {
      setPreview(null);
      setPending(false);
      return;
    }
    let cancelled = false;
    setPending(true);
    const t = setTimeout(() => {
      api<ExpensePreview>(`/groups/${publicId}/expenses/preview`, {
        method: "POST",
        body: { ...body, expense_id: expenseId },
      })
        .then((p) => {
          if (cancelled) return;
          setPreview(p);
          setError(null);
        })
        .catch((e) => !cancelled && setError(errorMessage(e)))
        .finally(() => !cancelled && setPending(false));
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [body, publicId, expenseId]);

  // pending の間は表示中のプレビューが古い可能性があるため、呼び出し側で確定ボタンを無効にする
  return { preview, error, pending };
}
