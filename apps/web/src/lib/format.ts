const yen = new Intl.NumberFormat("ja-JP");

export function formatYen(minor: number): string {
  if (!Number.isSafeInteger(minor)) return "—";
  return `${minor < 0 ? "−" : ""}${yen.format(Math.abs(minor))}円`;
}

/** 入力欄の文字列を整数円に。全角数字・カンマを許容し、整数以外は null。 */
export function parseYen(input: string): number | null {
  const normalized = input
    .replace(/[０-９]/g, (c) => String.fromCharCode(c.charCodeAt(0) - 0xfee0))
    .replace(/[,，\s円]/g, "")
    .replace(/^[−ー－]/, "-");
  if (!/^-?\d+$/.test(normalized)) return null;
  const n = Number(normalized);
  return Number.isSafeInteger(n) ? n : null;
}

export const STATUS_LABEL: Record<string, string> = {
  unpaid: "未払い",
  reported_paid: "送金申告済み(未確認)",
  recipient_confirmed: "受取人が確認済み",
  superseded: "新しい精算案に置き換え済み",
};

export const SPLIT_MODE_LABEL: Record<string, string> = {
  equal: "均等",
  weight: "比率",
  units: "数量",
  percentage: "割合",
  exact: "金額指定",
  itemized: "商品別",
};

export const ADJUSTMENT_KIND_LABEL: Record<string, string> = {
  tax: "税",
  discount: "割引",
  coupon: "クーポン",
  service_charge: "サービス料",
  rounding: "端数調整",
  other: "その他(差額調整)",
};

/** 調整額の入力欄の符号。割引・クーポンは正の数で入力させ、送信時に負へ変換する。 */
export const ADJUSTMENT_SIGN: Record<string, "positive" | "negative" | "any"> = {
  tax: "positive",
  service_charge: "positive",
  discount: "negative",
  coupon: "negative",
  rounding: "any",
  other: "any",
};

/** % 表記(小数第2位まで)を basis point の整数へ。不正なら null。 */
export function parsePercentBp(input: string): number | null {
  const m = input.trim().match(/^(\d+)(?:\.(\d{1,2}))?$/);
  return m ? Number(m[1]) * 100 + Number((m[2] ?? "").padEnd(2, "0")) : null;
}

export function formatBp(bp: number): string {
  return (bp / 100).toString();
}

/** 支出のカテゴリ(未選択 = 未分類)。サーバーの Category と同じ値 */
export const CATEGORY_LABEL: Record<string, string> = {
  food: "食事",
  drinks: "飲み会",
  groceries: "食材・買い物",
  transport: "交通",
  lodging: "宿泊",
  entertainment: "レジャー",
  daily: "日用品",
  other: "その他",
};

export function categoryLabel(c: string | null | undefined): string {
  return c ? (CATEGORY_LABEL[c] ?? c) : "未分類";
}
