import type { TransferRow } from "@/lib/api";

/** 送金の状態の短い表示 */
export function statusPill(t: TransferRow): { label: string; tone: "muted" | "primary" | "success" } {
  if (t.status === "reported_paid") return { label: "送金の申告あり", tone: "primary" };
  if (t.status === "recipient_confirmed") return { label: "受け取り済み", tone: "success" };
  return { label: "未送金", tone: "muted" };
}
