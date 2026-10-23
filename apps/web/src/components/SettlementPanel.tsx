"use client";

import { useCallback, useEffect, useState } from "react";
import {
  api,
  errorMessage,
  type Batch,
  type Group,
  type PayerDestination,
  type SettlementPlan,
  type TransferRow,
} from "@/lib/api";
import { formatYen } from "@/lib/format";
import { memberNames } from "@/lib/group-context";

const STEPS = ["unpaid", "reported_paid", "recipient_confirmed"] as const;

const TYPE_LABEL: Record<string, string> = { ordinary: "普通", checking: "当座", savings: "貯蓄" };

// 支払者だけに表示する振込先。アプリは送金しない(表示とコピーのみ)
function DestinationView({ transfer }: { transfer: TransferRow }) {
  const [d, setD] = useState<PayerDestination | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  async function copy(label: string, value: string) {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(label);
    } catch {
      setError("コピーできませんでした。手動で入力してください");
    }
  }

  if (!d) {
    return (
      <>
        <button
          type="button"
          className="secondary"
          onClick={() =>
            api<PayerDestination>(`/settlements/${transfer.id}/destination`)
              .then(setD)
              .catch((e) => setError(errorMessage(e)))
          }
        >
          振込先を表示
        </button>
        {error && <span className="error" role="alert">{error}</span>}
      </>
    );
  }
  if (!d.available || !d.details) {
    return <p className="muted">受取人は振込先を共有していません。受取人に直接確認してください。</p>;
  }
  const x = d.details as Record<string, string>;
  const rows: [string, string][] =
    d.kind === "bank"
      ? [
          ["銀行", `${x.bank_name}(${x.bank_code})`],
          ["支店", `${x.branch_name}(${x.branch_code})`],
          ["種類", TYPE_LABEL[x.account_type] ?? x.account_type],
          ["口座番号", x.account_number],
          ["名義", x.holder_kana],
        ]
      : [["PayPay ID", x.paypay_id]];
  return (
    <div className="card" aria-label="振込先">
      {d.changed_since_first_view && (
        <p className="notice warn" role="alert">
          受取人が振込先を変更しました。以前に表示した振込先({d.first_viewed_masked_label})ではなく、下の振込先を確認してください。
        </p>
      )}
      <dl>
        {rows.map(([k, v]) => (
          <div key={k} className="row">
            <dt>{k}</dt>
            <dd>{v}</dd>
            {(k === "口座番号" || k === "PayPay ID") && (
              <button type="button" className="secondary" onClick={() => copy(k, v)}>
                {copied === k ? "コピーしました" : `${k}をコピー`}
              </button>
            )}
          </div>
        ))}
        <div className="row">
          <dt>照合コード</dt>
          <dd>{d.reference_code}</dd>
          <button type="button" className="secondary" onClick={() => copy("照合コード", d.reference_code ?? "")}>
            {copied === "照合コード" ? "コピーしました" : "照合コードをコピー"}
          </button>
        </div>
      </dl>
      <p className="muted">{d.notice}</p>
      {error && <p className="error" role="alert">{error}</p>}
    </div>
  );
}

function ProgressSummary({ p }: { p: Batch["progress"] }) {
  return (
    <ul className="list" aria-label="送金の進み具合">
      <li>
        受取確認済み {p.recipient_confirmed_count}件({formatYen(p.recipient_confirmed_minor)})
      </li>
      <li>
        <span className={p.reported_unconfirmed_count > 0 ? "badge warn" : ""}>
          送金申告済み・受取確認待ち {p.reported_unconfirmed_count}件({formatYen(p.reported_unconfirmed_minor)})
        </span>
      </li>
      <li>
        未払い {p.unpaid_count}件({formatYen(p.unpaid_minor)})
      </li>
    </ul>
  );
}

function StatusTrack({ status }: { status: TransferRow["status"] }) {
  const at = STEPS.indexOf(status as (typeof STEPS)[number]);
  return (
    <ol className="track" aria-label="支払いの状況">
      {["精算確定", "送金申告", "受取確認"].map((label, i) => (
        <li key={label} className={i <= at ? "done" : ""} aria-current={i === at ? "step" : undefined}>
          {label}
        </li>
      ))}
    </ol>
  );
}

export function SettlementPanel({ group }: { group: Group }) {
  const publicId = group.public_id;
  const me = group.my_member_id;
  const [plan, setPlan] = useState<SettlementPlan | null>(null);
  const [batch, setBatch] = useState<Batch | null>(null);
  const [history, setHistory] = useState<Batch[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const names = memberNames(group);

  const load = useCallback(async () => {
    try {
      const [p, b, h] = await Promise.all([
        api<SettlementPlan>(`/groups/${publicId}/settlements/preview`, { method: "POST" }),
        api<Batch | null>(`/groups/${publicId}/settlement-batches/current`),
        api<Batch[]>(`/groups/${publicId}/settlement-batches`),
      ]);
      setPlan(p);
      setBatch(b);
      setHistory(h.filter((x) => x.state !== "active"));
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [publicId]);

  useEffect(() => {
    load();
  }, [load]);

  async function run(fn: () => Promise<unknown>) {
    setError(null);
    try {
      await fn();
    } catch (e) {
      setError(errorMessage(e));
    }
    await load();
  }

  async function copy(t: TransferRow) {
    try {
      await navigator.clipboard.writeText(String(t.amount_minor));
      setCopied(t.id);
    } catch {
      setError("コピーできませんでした。金額を手動で入力してください");
    }
  }

  const opt = plan?.optimization;
  const showPlan = plan && (!batch || batch.is_stale);
  const a = opt?.assumptions;

  return (
    <>
      <section className="card" aria-labelledby="settle-h">
        <h2 id="settle-h">精算</h2>
        {plan && opt && (
          <>
            <p>
              <span className={`chip ${opt.is_exact_minimum ? "exact" : "reduced"}`}>{opt.display_label}</span>{" "}
              {opt.optimized_transfer_count > 0 && (
                <span className="muted">
                  個別に払う場合 {opt.naive_transfer_count}件 → {opt.optimized_transfer_count}件
                </span>
              )}
            </p>
            {batch?.is_stale && (
              <p className="notice warn">確定後に支出が変更されました。精算案を更新してください。</p>
            )}
            {showPlan && plan.transfers.length > 0 && (
              <ul className="list" aria-label="精算案">
                {plan.transfers.map((t, i) => (
                  <li key={i}>
                    <span>{names[t.from_member_id]} → {names[t.to_member_id]}</span>
                    <span className="amount">{formatYen(t.amount_minor)}</span>
                  </li>
                ))}
              </ul>
            )}
            {showPlan && plan.transfers.length > 0 && (
              <p className="row actions">
                <button
                  type="button"
                  onClick={() =>
                    run(() =>
                      api(`/groups/${publicId}/settlement-batches`, {
                        method: "POST",
                        body: { expected_ledger_version: plan.ledger_version },
                      }),
                    )
                  }
                >
                  {batch?.is_stale ? "支出の変更を反映して精算案を更新" : "この精算案で確定"}
                </button>
              </p>
            )}
          </>
        )}

        {batch && !batch.is_stale && <ProgressSummary p={batch.progress} />}
        {batch && !batch.is_stale && batch.progress.reported_unconfirmed_count > 0 && (
          <p className="muted">
            「受取確認待ち」は支払う人の申告だけで、受け取る人はまだ確認していません。精算案を計算し直すときは支払い済みとして扱いますが、着金を確かめたものではありません。
          </p>
        )}
        {batch && !batch.is_stale && (
          <ul className="list" aria-label="確定した精算">
            {batch.transfers
              .filter((t) => t.status !== "superseded")
              .map((t) => (
                <li key={t.id} className="transfer">
                  <span>
                    {names[t.from_member_id]} → {names[t.to_member_id]}{" "}
                    <span className="amount">{formatYen(t.amount_minor)}</span>
                    <br />
                    <span className={t.status === "reported_paid" ? "badge warn" : "muted"}>{t.status_label}</span>
                    <StatusTrack status={t.status} />
                  </span>
                  <span className="row" style={{ flex: "0 0 auto" }}>
                    {t.from_member_id === me && t.status === "unpaid" && (
                      <>
                        <button type="button" className="secondary" onClick={() => copy(t)}>
                          {copied === t.id ? "コピーしました" : "金額をコピー"}
                        </button>
                        <button type="button" onClick={() => run(() => api(`/settlements/${t.id}/report-paid`, { method: "POST" }))}>
                          送金したと申告
                        </button>
                      </>
                    )}
                    {t.from_member_id === me && (t.status === "unpaid" || t.status === "reported_paid") && (
                      <DestinationView transfer={t} />
                    )}
                    {t.to_member_id === me && t.status !== "recipient_confirmed" && (
                      <button type="button" onClick={() => run(() => api(`/settlements/${t.id}/confirm-received`, { method: "POST" }))}>
                        受け取りを確認
                      </button>
                    )}
                  </span>
                </li>
              ))}
          </ul>
        )}
        {error && <p className="error" role="alert">{error}</p>}
      </section>

      <section className="card" aria-labelledby="note-h">
        <h2 id="note-h">送金について</h2>
        <p>このアプリは送金を実行しません。送金は各自が銀行アプリや PayPay などで行ってください。</p>
        <p className="muted">
          「送金申告済み」は支払う人の自己申告で、銀行での確認ではありません。「受取人が確認済み」は受け取る人の手動確認です。
          いずれも支払いを保証するものではありません。
        </p>
        {a && (
          <details>
            <summary>精算案の計算の前提</summary>
            <ul>
              <li>誰から誰へでも送金できるものとして計算しています</li>
              <li>通貨は{a.currency}のみ、1円単位の整数で計算しています</li>
              <li>振込手数料は考慮していません</li>
              <li>1回あたりの送金上限は{a.per_transfer_limit_minor === null ? "考慮していません" : formatYen(a.per_transfer_limit_minor)}</li>
              <li>
                送金申告済み(受取確認待ちを含む)・受取確認済みの送金は変更せず、残りだけを計算し直します
              </li>
              {opt && !opt.is_exact_minimum && opt.optimized_transfer_count > 0 && (
                <li>人数が多いため、送金回数が最小であることは保証していません</li>
              )}
            </ul>
          </details>
        )}
      </section>

      {history.length > 0 && (
        <section className="card" aria-labelledby="hist-h">
          <h2 id="hist-h">以前の精算案</h2>
          <ul className="list">
            {history.map((h) => (
              <li key={h.id}>
                <span>{new Date(h.created_at).toLocaleString("ja-JP")}</span>
                <span className="muted">{h.transfers.length}件(置き換え済み)</span>
              </li>
            ))}
          </ul>
        </section>
      )}
    </>
  );
}
