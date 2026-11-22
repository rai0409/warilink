"use client";

import { useEffect, useMemo, useState } from "react";
import { api, errorMessage, type ExpenseBody, type ExpensePreview } from "@/lib/api";

/** サーバーで負担額を計算する。入力や再試行ごとに結果を区別する。 */
export function useExpensePreview(publicId: string, body: ExpenseBody | null, expenseId: string) {
  const [attempt, setAttempt] = useState(0);
  const identity = JSON.stringify([publicId, expenseId, body]);
  // 同じ入力に戻った場合も、新しい token で前の成功結果と区別する。
  const request = useMemo(() => ({ identity, attempt }), [identity, attempt]);
  const [result, setResult] = useState<{
    request: typeof request;
    preview: ExpensePreview | null;
    error: string | null;
  } | null>(null);

  useEffect(() => {
    const [group, id, requestBody] = JSON.parse(request.identity) as [string, string, ExpenseBody | null];
    if (!requestBody) return;
    let cancelled = false;
    const t = setTimeout(() => {
      api<ExpensePreview>(`/groups/${group}/expenses/preview`, {
        method: "POST",
        body: { ...requestBody, expense_id: id },
      })
        .then((preview) => {
          if (!cancelled) setResult({ request, preview, error: null });
        })
        .catch((e) => {
          if (!cancelled) setResult({ request, preview: null, error: errorMessage(e) });
        });
    }, 250);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
  }, [request]);

  const current = body && result?.request === request ? result : null;
  return {
    preview: current?.preview ?? null,
    error: current?.error ?? null,
    pending: Boolean(body && !current),
    retry: () => setAttempt((n) => n + 1),
  };
}
