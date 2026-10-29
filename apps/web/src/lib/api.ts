// FastAPI への薄いクライアント。金額計算はすべてサーバーが正本で、ここでは計算しない。
import type { Schemas } from "@warilink/contracts";
import { uuid } from "./uuid";

export type Group = Schemas["GroupOut"];
export type Member = Schemas["MemberOut"];
export type Expense = Schemas["ExpenseOut"];
export type ExpenseInput = Schemas["ExpenseCreate"];
export type Breakdown = Schemas["BreakdownOut"];
export type ExpensePreview = Schemas["ExpensePreviewOut"];
export type TransferRow = Schemas["TransferOut"];
export type SettlementPlan = Schemas["SettlementPlanOut"];
export type Batch = Schemas["BatchOut"];
export type ExpenseDetail = Schemas["ExpenseDetailOut"];
export type ExpenseBody = Schemas["ExpenseIn"];
export type Revision = Schemas["RevisionOut"];
export type Issue = Schemas["IssueOut"];
export type Plans = Schemas["PlansOut"];
export type Receipt = Schemas["ReceiptOut"];
export type Quota = Schemas["QuotaOut"];

export class ApiProblem extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public body: Record<string, unknown>,
  ) {
    super(message);
  }
}

const FALLBACK_MESSAGE = "通信に失敗しました。時間をおいて再度お試しください";

export async function api<T>(
  path: string,
  init: { method?: string; body?: unknown; form?: FormData; idempotencyKey?: string; ifMatch?: number } = {},
): Promise<T> {
  const method = init.method ?? "GET";
  const headers: Record<string, string> = { "X-WariLink-CSRF": "1" };
  if (init.body !== undefined) headers["Content-Type"] = "application/json";
  if (method === "POST") headers["Idempotency-Key"] = init.idempotencyKey ?? uuid();
  if (init.ifMatch !== undefined) headers["If-Match"] = String(init.ifMatch);
  let res: Response;
  try {
    res = await fetch(`/api/v1${path}`, {
      method,
      headers,
      credentials: "same-origin",
      cache: "no-store",
      body: init.form ?? (init.body === undefined ? undefined : JSON.stringify(init.body)),
    });
  } catch {
    throw new ApiProblem(0, "NETWORK", FALLBACK_MESSAGE, {});
  }
  const text = await res.text();
  const data = text ? JSON.parse(text) : null;
  if (!res.ok) {
    // サーバーは日本語の detail を返す。技術的な文字列はそのまま出さない。
    const detail = typeof data?.detail === "string" ? data.detail : FALLBACK_MESSAGE;
    throw new ApiProblem(res.status, data?.code ?? "UNKNOWN", detail, data ?? {});
  }
  return data as T;
}

export async function ensureGuest(): Promise<void> {
  await api("/identity/guest", { method: "POST" });
}

export function errorMessage(e: unknown): string {
  return e instanceof ApiProblem ? e.message : FALLBACK_MESSAGE;
}

export type Me = Schemas["IdentityOut"];
export type SessionState = Schemas["SessionStateOut"];
export type Passkey = Schemas["PasskeyOut"];
export type PasskeyOptions = Schemas["OptionsOut"];

export type Destination = Schemas["DestinationOut"];
export type DestinationReveal = Schemas["DestinationRevealOut"];
export type DestinationInput = Schemas["DestinationIn"];
export type PayerDestination = Schemas["PayerDestinationOut"];

export type ExpenseSearch = Schemas["ExpenseSearchOut"];
export type GroupStats = Schemas["GroupStatsOut"];
export type GroupSummary = Schemas["GroupSummaryOut"];
export type InviteStatus = Schemas["InviteStatusOut"];
