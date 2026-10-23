import type { AdjDraft, InitialDraft, ItemDraft, ReviewMark } from "@/components/ReceiptEditor";

// サーバー(パーサー)の下書き JSON。読み取り結果であり確定値ではない。
export type OcrItem = {
  id: string; name: string; quantity: number; line_minor: number; tax_group: "8" | "10" | null;
  confidence: number; needs_review: boolean; reasons: string[]; bbox: number[];
};
export type OcrAdjustment = {
  id: string; kind: AdjDraft["kind"]; label: string; amount_minor: number; tax_group: "8" | "10" | null;
  scope_item_ids: string[] | null; confidence: number; needs_review: boolean; bbox: number[];
  reasons?: string[]; // rule-ja-0.2.0 以降。古い下書きには無い
};
export type OcrDraft = {
  store_name: string | null; purchased_on: string | null; items: OcrItem[]; adjustments: OcrAdjustment[];
  total_minor: number | null; tax_mode: string; difference_minor: number | null; image_scale: number;
  warnings: { code: string; message: string }[];
  total_candidates?: { value: number; line: number }[]; // rule-ja-0.2.0 以降
  item_count_printed?: number | null;
};

/** 読み取り結果を編集用の下書きへ。負担者は未選択のまま(利用者が選ぶ)。 */
export function draftToEditor(d: OcrDraft): { initial: InitialDraft; review: Record<string, ReviewMark> } {
  const groups = new Set(d.items.map((i) => i.tax_group).filter(Boolean));
  const items: ItemDraft[] = d.items.map((i) => ({
    id: i.id, name: i.name, qty: String(i.quantity), line: String(i.line_minor), tax: i.tax_group ?? "",
    mode: "equal", members: [], values: {},
  }));
  const adjustments: AdjDraft[] = d.adjustments.map((a) => ({
    id: a.id, kind: a.kind, label: a.label,
    amount: String(a.kind === "discount" || a.kind === "coupon" ? Math.abs(a.amount_minor) : a.amount_minor),
    rule: "proportional",
    // 税率ごとの税は、その税率の商品があるときだけ対象を絞る(無ければ配分先がなく確定できないため全商品)
    scope: a.scope_item_ids?.length ? "items" : a.tax_group && groups.has(a.tax_group) ? a.tax_group : "all",
    scopeItemIds: a.scope_item_ids ?? undefined,
    members: [],
  }));
  const review: Record<string, ReviewMark> = {};
  for (const i of d.items) if (i.needs_review) review[i.id] = { confidence: i.confidence, reasons: i.reasons };
  for (const a of d.adjustments) {
    if (a.needs_review) {
      review[a.id] = {
        confidence: a.confidence,
        // 新しいパーサーは理由を返す。古い下書き(rule-ja-0.1.0)は確信度と対象から推定する
        reasons: a.reasons?.length
          ? a.reasons
          : [...(a.confidence < 0.9 ? ["LOW_CONFIDENCE"] : []), ...(a.scope_item_ids ? ["SCOPED_DISCOUNT"] : [])],
      };
    }
  }
  return {
    initial: { title: d.store_name ?? "レシート", total: d.total_minor === null ? "" : String(d.total_minor), items,
      adjustments },
    review,
  };
}
