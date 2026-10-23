"use client";

import { useMemo, useState } from "react";
import { ApiProblem, api, errorMessage, type ExpenseBody, type ExpenseDetail, type Issue, type Member } from "@/lib/api";
import { ADJUSTMENT_KIND_LABEL, ADJUSTMENT_SIGN, formatYen, parseYen } from "@/lib/format";
import { useExpensePreview } from "@/lib/use-preview";
import { IssueList, PreviewShares } from "@/components/PreviewShares";

type AllocMode = "equal" | "units" | "exact";
type TaxGroup = "" | "8" | "10";
type AdjKind = "tax" | "discount" | "coupon" | "service_charge" | "rounding" | "other";
type RuleType = "proportional" | "equal" | "members";

export type ItemDraft = {
  id: string;
  name: string;
  qty: string;
  line: string;
  tax: TaxGroup;
  mode: AllocMode;
  members: string[]; // equal のときの負担者
  values: Record<string, string>; // units: 個数 / exact: 金額
};

export type AdjDraft = {
  id: string;
  kind: AdjKind;
  label: string;
  amount: string; // 割引・クーポンは正の数で入力(送信時に負へ)
  rule: RuleType;
  scope: "all" | "8" | "10" | "items";
  scopeItemIds?: string[]; // scope=items のとき(読み取り結果で「直前の商品の値引き」と判定した場合)
  members: string[];
};

/** 読み取り結果の確認用: 行ごとの確信度と要確認の理由 */
export type ReviewMark = { confidence: number; reasons: string[] };

export type InitialDraft = { title: string; total: string; payer?: string; items: ItemDraft[]; adjustments: AdjDraft[] };

const REASON_LABEL: Record<string, string> = {
  LOW_CONFIDENCE: "読み取りの確信度が低い",
  QTY_PRICE_INCONSISTENT: "単価×数量と金額が合わない",
  NAME_MISSING: "商品名を読み取れなかった",
  SCOPED_DISCOUNT: "直前の商品だけの値引きと判断",
  POINT_USAGE: "ポイント利用(誰の負担を減らすか確認してください)",
  REFUND: "返品・取消の行",
};

const ALLOC_LABEL: Record<AllocMode, string> = { equal: "均等", units: "数量で分ける", exact: "金額指定" };
const RULE_LABEL: Record<RuleType, string> = {
  proportional: "負担額に比例",
  equal: "対象商品の負担者で均等",
  members: "指定した人で均等",
};

const newId = (p: string) => `${p}-${crypto.randomUUID().slice(0, 8)}`;
const emptyItem = (): ItemDraft => ({
  id: newId("i"), name: "", qty: "1", line: "", tax: "", mode: "equal", members: [], values: {},
});

type ReceiptSpec = {
  title: string;
  amount_minor: number;
  payers: { member_id: string; amount_minor: number }[];
  split: {
    receipt: {
      receipt_total_minor: number;
      items: {
        id: string; name: string; line_minor: number; quantity: number; tax_group: "8" | "10" | null;
        allocation: { mode: string; shares: { member_id: string; value: number }[] } | null;
      }[];
      adjustments: {
        id: string; kind: AdjKind; label: string; amount_minor: number;
        rule: { type: string; scope_tax_group?: "8" | "10" | null; scope_item_ids?: string[] | null;
        member_ids?: string[] | null };
      }[];
    };
  };
};

/** 保存済みの支出(商品別)を編集用の下書きへ戻す。 */
function fromSpec(spec: ReceiptSpec) {
  const r = spec.split.receipt;
  const items: ItemDraft[] = r.items.map((it) => {
    const a = it.allocation;
    const mode: AllocMode = a?.mode === "units" ? "units" : a?.mode === "exact" ? "exact" : "equal";
    return {
      id: it.id, name: it.name, qty: String(it.quantity), line: String(it.line_minor), tax: it.tax_group ?? "",
      mode,
      members: mode === "equal" ? (a?.shares ?? []).map((s) => s.member_id) : [],
      values: mode === "equal" ? {} : Object.fromEntries((a?.shares ?? []).map((s) => [s.member_id, String(s.value)])),
    };
  });
  const adjustments: AdjDraft[] = r.adjustments.map((a) => ({
    id: a.id, kind: a.kind, label: a.label,
    amount: String(ADJUSTMENT_SIGN[a.kind] === "negative" ? -a.amount_minor : a.amount_minor),
    rule: (a.rule.type === "equal" || a.rule.type === "members" ? a.rule.type : "proportional") as RuleType,
    scope: a.rule.scope_item_ids?.length ? "items" : (a.rule.scope_tax_group ?? "all"),
    scopeItemIds: a.rule.scope_item_ids ?? undefined,
    members: a.rule.member_ids ?? [],
  }));
  return { title: spec.title, total: String(r.receipt_total_minor), payer: spec.payers[0]?.member_id, items, adjustments };
}

type Props = {
  publicId: string;
  members: Member[];
  myMemberId: string;
  editing?: ExpenseDetail;
  /** 読み取り結果から始める場合の下書き・要確認の印・元のレシート */
  initialDraft?: InitialDraft;
  review?: Record<string, ReviewMark>;
  sourceReceiptId?: string;
  onFocusLine?: (id: string | null) => void;
  onSaved: (expenseId: string) => void;
};

/**
 * 商品別割り勘。商品ごとに負担者を選び、税・割引などは明示したルールで配分する。
 * 金額の計算・検算はすべてサーバー(プレビュー API)が行い、ここでは入力と表示だけを扱う。
 */
export function ReceiptEditor({
  publicId, members, myMemberId, editing, initialDraft, review, sourceReceiptId, onFocusLine, onSaved,
}: Props) {
  const init = useMemo(
    () => (editing ? fromSpec(editing.spec as unknown as ReceiptSpec) : initialDraft ?? null),
    [editing, initialDraft],
  );
  const [startedAt] = useState(() => Date.now());
  const [title, setTitle] = useState(init?.title ?? "");
  const [totalText, setTotalText] = useState(init?.total ?? "");
  const [payer, setPayer] = useState(init?.payer ?? myMemberId);
  const [items, setItems] = useState<ItemDraft[]>(init?.items ?? [emptyItem()]);
  const [adjs, setAdjs] = useState<AdjDraft[]>(init?.adjustments ?? []);
  const [bulk, setBulk] = useState<Set<string>>(new Set());
  const [bulkMembers, setBulkMembers] = useState<Set<string>>(new Set());
  const [expenseId, setExpenseId] = useState(() => editing?.id ?? crypto.randomUUID());
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const total = parseYen(totalText);
  const allIds = members.map((m) => m.id);

  // 入力の形式チェック(数値として読めるか)だけを画面で行う。金額の整合性はサーバーが判定する。
  const localErrors = useMemo(() => {
    const errs: Record<string, string> = {};
    for (const it of items) {
      if (parseYen(it.line) === null) errs[it.id] = "金額は整数の円で入力してください(返品はマイナス)";
      else if (!/^\d+$/.test(it.qty) || Number(it.qty) < 1) errs[it.id] = "数量は1以上の整数で入力してください";
    }
    for (const a of adjs) {
      const v = parseYen(a.amount);
      if (v === null || (ADJUSTMENT_SIGN[a.kind] !== "any" && v < 0)) errs[a.id] = "金額は0以上の整数の円で入力してください";
    }
    return errs;
  }, [items, adjs]);

  const body = useMemo((): ExpenseBody | null => {
    if (total === null || !title.trim() || Object.keys(localErrors).length > 0) return null;
    const receiptItems = items.map((it) => {
      let shares: { member_id: string; value: number }[] = [];
      if (it.mode === "equal") shares = it.members.map((id) => ({ member_id: id, value: 1 }));
      else
        shares = Object.entries(it.values)
          .map(([id, raw]) => ({ member_id: id, value: parseYen(raw) ?? -1 }))
          .filter((s) => s.value !== 0);
      return {
        id: it.id,
        name: it.name.trim(),
        line_minor: parseYen(it.line)!,
        quantity: Number(it.qty),
        tax_group: it.tax === "" ? null : it.tax,
        allocation: shares.length ? { mode: it.mode, shares } : null,
      };
    });
    const adjustments = adjs.map((a) => {
      const v = parseYen(a.amount)!;
      const rule =
        a.rule === "members"
          ? { type: "members" as const, member_ids: a.members }
          : a.scope === "items"
            ? { type: a.rule, scope_item_ids: a.scopeItemIds ?? [] }
            : { type: a.rule, scope_tax_group: a.scope === "all" ? null : a.scope };
      return {
        id: a.id,
        kind: a.kind,
        label: a.label.trim(),
        amount_minor: ADJUSTMENT_SIGN[a.kind] === "negative" ? -v : v,
        rule,
      };
    });
    return {
      title: title.trim(),
      amount_minor: total,
      payers: [{ member_id: payer, amount_minor: total }],
      split: { mode: "itemized", shares: [], receipt: { receipt_total_minor: total, items: receiptItems, adjustments } },
    };
  }, [total, title, localErrors, items, adjs, payer]);

  const { preview, error: previewError, pending } = useExpensePreview(publicId, body, expenseId);
  const bd = preview?.breakdown;

  const issuesFor = (id: string): Issue[] => (bd?.issues ?? []).filter((i) => i.target === id);
  const mismatch = bd?.issues.find((i) => i.code === "RECEIPT_TOTAL_MISMATCH" && i.detail?.difference !== undefined)
    ?.detail as { breakdown_total: number; receipt_total: number; difference: number } | undefined;
  const allocOf = (kind: "item" | "adj", id: string) =>
    (kind === "item" ? bd?.item_allocations : bd?.adjustment_allocations)?.find((a) => a.id === id)?.allocations;

  function patchItem(id: string, patch: Partial<ItemDraft>) {
    setItems((xs) => xs.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  }
  function patchAdj(id: string, patch: Partial<AdjDraft>) {
    setAdjs((xs) => xs.map((x) => (x.id === id ? { ...x, ...patch } : x)));
  }
  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }
  function copyPrevious(index: number) {
    const prev = items[index - 1];
    patchItem(items[index].id, { mode: prev.mode, members: [...prev.members], values: { ...prev.values } });
  }
  function applyBulk() {
    const ids = allIds.filter((id) => bulkMembers.has(id));
    setItems((xs) => xs.map((x) => (bulk.has(x.id) ? { ...x, mode: "equal", members: ids, values: {} } : x)));
    setBulk(new Set());
  }
  function addDifferenceLine(diff: number) {
    // ユーザーが明示的に押したときだけ差額調整行を追加する(自動補正はしない)
    setAdjs((xs) => [
      ...xs,
      { id: newId("a"), kind: "other", label: "差額調整", amount: String(diff), rule: "proportional", scope: "all", members: [] },
    ]);
  }

  async function save() {
    if (!body) return;
    setBusy(true);
    setSaveError(null);
    try {
      if (editing) {
        await api(`/groups/${publicId}/expenses/${editing.id}`, { method: "PUT", ifMatch: editing.version, body });
        onSaved(editing.id);
      } else {
        await api(`/groups/${publicId}/expenses`, {
          method: "POST",
          idempotencyKey: expenseId,
          body: {
            ...body,
            client_expense_id: expenseId,
            ...(sourceReceiptId
              ? { source_receipt_id: sourceReceiptId, review_elapsed_ms: Math.min(Date.now() - startedAt, 86_400_000) }
              : {}),
          },
        });
        const saved = expenseId;
        setExpenseId(crypto.randomUUID());
        onSaved(saved);
      }
    } catch (e) {
      setSaveError(
        e instanceof ApiProblem && e.code === "REVISION_CONFLICT"
          ? `${e.message}(画面を再読み込みすると最新の内容が表示されます)`
          : errorMessage(e),
      );
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card" aria-labelledby="receipt-h">
      <h2 id="receipt-h">{editing ? "商品別の支出を編集" : "レシートの内容を入力"}</h2>
      <label htmlFor="r-title">店名・内容</label>
      <input id="r-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={100} />
      <label htmlFor="r-total">レシートの合計金額(円)</label>
      <input id="r-total" inputMode="numeric" value={totalText} onChange={(e) => setTotalText(e.target.value)} />
      {totalText && total === null && <p className="error">合計金額は整数の円で入力してください</p>}
      <label htmlFor="r-payer">支払った人</label>
      <select id="r-payer" value={payer} onChange={(e) => setPayer(e.target.value)}>
        {members.map((m) => (
          <option key={m.id} value={m.id}>{m.display_name}</option>
        ))}
      </select>

      <h3>商品</h3>
      {items.map((it, index) => {
        const issues = issuesFor(it.id);
        const unallocated = issues.some((i) => i.code === "ITEM_UNALLOCATED");
        const alloc = allocOf("item", it.id);
        const unitSum = it.mode === "units"
          ? Object.values(it.values).reduce((s, v) => s + (Number(v) || 0), 0)
          : 0;
        return (
          <fieldset
            key={it.id}
            className={`item${issues.length || localErrors[it.id] ? " has-issue" : ""}${review?.[it.id] ? " needs-review" : ""}`}
            aria-label={`商品${index + 1}`}
            onFocus={() => onFocusLine?.(it.id)}
          >
            <legend>
              商品{index + 1}
              {unallocated && <span className="badge warn">未割当</span>}
              {review?.[it.id] && <span className="badge review">要確認</span>}
            </legend>
            <ReviewNote mark={review?.[it.id]} />
            <div className="item-grid">
              <div>
                <label htmlFor={`${it.id}-name`}>商品名</label>
                <input id={`${it.id}-name`} value={it.name} maxLength={100}
                  onChange={(e) => patchItem(it.id, { name: e.target.value })} />
              </div>
              <div>
                <label htmlFor={`${it.id}-qty`}>数量</label>
                <input id={`${it.id}-qty`} inputMode="numeric" value={it.qty}
                  onChange={(e) => patchItem(it.id, { qty: e.target.value })} />
              </div>
              <div>
                <label htmlFor={`${it.id}-line`}>金額(円)</label>
                <input id={`${it.id}-line`} inputMode="numeric" value={it.line}
                  onChange={(e) => patchItem(it.id, { line: e.target.value })} />
              </div>
              <div>
                <label htmlFor={`${it.id}-tax`}>税率</label>
                <select id={`${it.id}-tax`} value={it.tax}
                  onChange={(e) => patchItem(it.id, { tax: e.target.value as TaxGroup })}>
                  <option value="">指定なし</option>
                  <option value="8">8%(軽減)</option>
                  <option value="10">10%</option>
                </select>
              </div>
            </div>

            <div className="row">
              <label className="check">
                <input type="checkbox" checked={bulk.has(it.id)}
                  onChange={() => setBulk((s) => { const n = new Set(s); if (n.has(it.id)) n.delete(it.id); else n.add(it.id); return n; })} />
                一括割当の対象
              </label>
              <select aria-label="分け方" value={it.mode}
                onChange={(e) => patchItem(it.id, { mode: e.target.value as AllocMode, values: {} })}>
                {(Object.keys(ALLOC_LABEL) as AllocMode[]).map((m) => (
                  <option key={m} value={m}>{ALLOC_LABEL[m]}</option>
                ))}
              </select>
            </div>

            {it.mode === "equal" && (
              <div className="row" role="group" aria-label="負担する人">
                <button type="button" className="toggle" onClick={() => patchItem(it.id, { members: allIds })}>全員</button>
                {members.map((m) => (
                  <button key={m.id} type="button" className="toggle" aria-pressed={it.members.includes(m.id)}
                    onClick={() => patchItem(it.id, { members: toggle(it.members, m.id) })}>
                    {m.display_name}
                  </button>
                ))}
              </div>
            )}
            {it.mode === "units" && (
              <div role="group" aria-label="数量の割当">
                {members.map((m) => {
                  const n = Number(it.values[m.id] ?? 0) || 0;
                  const set = (v: number) => patchItem(it.id, { values: { ...it.values, [m.id]: String(Math.max(0, v)) } });
                  return (
                    <div key={m.id} className="stepper">
                      <span>{m.display_name}</span>
                      <button type="button" className="secondary" aria-label={`${m.display_name}の数量を減らす`}
                        disabled={n === 0} onClick={() => set(n - 1)}>−</button>
                      <output aria-label={`${m.display_name}の数量`}>{n}</output>
                      <button type="button" className="secondary" aria-label={`${m.display_name}の数量を増やす`}
                        onClick={() => set(n + 1)}>+</button>
                    </div>
                  );
                })}
                <p className="muted">割当 {unitSum} / {it.qty}</p>
              </div>
            )}
            {it.mode === "exact" && (
              <div role="group" aria-label="金額の指定">
                {members.map((m) => (
                  <div key={m.id} className="inline-field">
                    <label htmlFor={`${it.id}-x-${m.id}`}>{m.display_name}の金額(円)</label>
                    <input id={`${it.id}-x-${m.id}`} inputMode="numeric" value={it.values[m.id] ?? ""}
                      onChange={(e) => patchItem(it.id, { values: { ...it.values, [m.id]: e.target.value } })} />
                  </div>
                ))}
              </div>
            )}

            {alloc && (
              <p className="muted" aria-label="この商品の負担">
                {members.filter((m) => alloc[m.id]).map((m) => `${m.display_name} ${formatYen(alloc[m.id])}`).join(" · ")}
              </p>
            )}
            {localErrors[it.id] && <p className="error">{localErrors[it.id]}</p>}
            {issues.filter((i) => i.code !== "ITEM_UNALLOCATED").map((i, n) => (
              <p key={n} className="error">{i.message}</p>
            ))}
            <p className="row">
              {index > 0 && (
                <button type="button" className="secondary" onClick={() => copyPrevious(index)}>前の商品と同じ</button>
              )}
              <button type="button" className="secondary" disabled={items.length === 1}
                onClick={() => setItems((xs) => xs.filter((x) => x.id !== it.id))}>
                この商品を削除
              </button>
            </p>
          </fieldset>
        );
      })}
      <p className="row">
        <button type="button" className="secondary" onClick={() => setItems((xs) => [...xs, emptyItem()])}>商品を追加</button>
      </p>

      {bulk.size > 0 && (
        <div className="bulk" role="region" aria-label="一括割当">
          <p>選択した{bulk.size}件の商品に、次の人を均等に割り当てます</p>
          <div className="row" role="group" aria-label="一括で負担する人">
            {members.map((m) => (
              <button key={m.id} type="button" className="toggle" aria-pressed={bulkMembers.has(m.id)}
                onClick={() => setBulkMembers((s) => { const n = new Set(s); if (n.has(m.id)) n.delete(m.id); else n.add(m.id); return n; })}>
                {m.display_name}
              </button>
            ))}
          </div>
          <p className="row">
            <button type="button" disabled={bulkMembers.size === 0} onClick={applyBulk}>選択した商品に割り当てる</button>
            <button type="button" className="secondary" onClick={() => setBulk(new Set())}>選択を解除</button>
          </p>
        </div>
      )}

      <h3>税・割引・サービス料</h3>
      <p className="muted">
        内税のレシートでは税をここに入れないでください(商品の金額に含まれているため二重計上になります)。
      </p>
      {adjs.map((a, index) => {
        const issues = issuesFor(a.id);
        const alloc = allocOf("adj", a.id);
        const sign = ADJUSTMENT_SIGN[a.kind];
        return (
          <fieldset key={a.id}
            className={`item${issues.length || localErrors[a.id] ? " has-issue" : ""}${review?.[a.id] ? " needs-review" : ""}`}
            aria-label={`調整${index + 1}`} onFocus={() => onFocusLine?.(a.id)}>
            <legend>
              調整{index + 1}
              {review?.[a.id] && <span className="badge review">要確認</span>}
            </legend>
            <ReviewNote mark={review?.[a.id]} />
            <div className="item-grid">
              <div>
                <label htmlFor={`${a.id}-kind`}>種類</label>
                <select id={`${a.id}-kind`} value={a.kind} onChange={(e) => patchAdj(a.id, { kind: e.target.value as AdjKind })}>
                  {Object.entries(ADJUSTMENT_KIND_LABEL).map(([k, v]) => (
                    <option key={k} value={k}>{v}</option>
                  ))}
                </select>
              </div>
              <div>
                <label htmlFor={`${a.id}-amount`}>
                  {sign === "negative" ? "差し引く金額(円)" : sign === "positive" ? "金額(円)" : "金額(円・マイナス可)"}
                </label>
                <input id={`${a.id}-amount`} inputMode="numeric" value={a.amount}
                  onChange={(e) => patchAdj(a.id, { amount: e.target.value })} />
              </div>
              <div>
                <label htmlFor={`${a.id}-rule`}>分け方</label>
                <select id={`${a.id}-rule`} value={a.rule} onChange={(e) => patchAdj(a.id, { rule: e.target.value as RuleType })}>
                  {(Object.keys(RULE_LABEL) as RuleType[]).map((r) => (
                    <option key={r} value={r}>{RULE_LABEL[r]}</option>
                  ))}
                </select>
              </div>
              {a.rule !== "members" && (
                <div>
                  <label htmlFor={`${a.id}-scope`}>対象の商品</label>
                  <select id={`${a.id}-scope`} value={a.scope}
                    onChange={(e) => patchAdj(a.id, { scope: e.target.value as AdjDraft["scope"] })}>
                    <option value="all">すべての商品</option>
                    <option value="8">税率8%の商品</option>
                    <option value="10">税率10%の商品</option>
                    {a.scopeItemIds?.length ? (
                      <option value="items">
                        {items.filter((x) => a.scopeItemIds!.includes(x.id)).map((x) => x.name || "商品").join("・")}のみ
                      </option>
                    ) : null}
                  </select>
                </div>
              )}
            </div>
            {a.rule === "members" && (
              <div className="row" role="group" aria-label="負担する人">
                {members.map((m) => (
                  <button key={m.id} type="button" className="toggle" aria-pressed={a.members.includes(m.id)}
                    onClick={() => patchAdj(a.id, { members: toggle(a.members, m.id) })}>
                    {m.display_name}
                  </button>
                ))}
              </div>
            )}
            {alloc && (
              <p className="muted" aria-label="この調整の負担">
                {members.filter((m) => alloc[m.id]).map((m) => `${m.display_name} ${formatYen(alloc[m.id])}`).join(" · ")}
              </p>
            )}
            {localErrors[a.id] && <p className="error">{localErrors[a.id]}</p>}
            {issues.map((i, n) => (
              <p key={n} className="error">{i.message}</p>
            ))}
            <p className="row">
              <button type="button" className="secondary" onClick={() => setAdjs((xs) => xs.filter((x) => x.id !== a.id))}>
                この調整を削除
              </button>
            </p>
          </fieldset>
        );
      })}
      <p className="row">
        <button type="button" className="secondary"
          onClick={() => setAdjs((xs) => [...xs, { id: newId("a"), kind: "tax", label: "", amount: "", rule: "proportional", scope: "all", members: [] }])}>
          税・割引などを追加
        </button>
      </p>

      <div aria-live="polite" className="preview">
        {mismatch && (
          <div className="notice warn" role="region" aria-label="合計金額の不一致">
            <p>
              明細と調整額の合計 {formatYen(mismatch.breakdown_total)} が、レシートの合計{" "}
              {formatYen(mismatch.receipt_total)} と一致しません(差額{" "}
              <strong>{formatYen(mismatch.difference)}</strong>)。
            </p>
            <p>金額を自動では補正しません。明細を訂正するか、差額を調整行として明示的に追加してください。</p>
            <button type="button" className="secondary" onClick={() => addDifferenceLine(mismatch.difference)}>
              差額 {formatYen(mismatch.difference)} を調整行として追加
            </button>
          </div>
        )}
        {bd && <IssueList issues={bd.issues.filter((i) => i.code !== "RECEIPT_TOTAL_MISMATCH")} onlyGlobal />}
        {bd?.ok && (
          <>
            <p className="ok">明細の合計がレシートの合計 {formatYen(bd.receipt_total_minor ?? 0)} と一致しました。</p>
            <PreviewShares members={members} shares={bd.shares} label="メンバー別の負担" />
          </>
        )}
        {preview?.projected_settlement && (
          <p className="muted">
            登録後の精算: {preview.projected_settlement.transfers.length}件(
            {preview.projected_settlement.optimization.display_label})
          </p>
        )}
      </div>
      {previewError && <p className="error" role="alert">{previewError}</p>}
      {saveError && <p className="error" role="alert">{saveError}</p>}
      <p className="row actions">
        <button type="button" disabled={!bd?.ok || pending || busy || !body} onClick={save}>
          {editing ? "変更を保存" : "この内容で登録"}
        </button>
      </p>
      {!bd?.ok && <p className="muted">すべての商品に負担者を選び、合計が一致すると登録できます。</p>}
    </section>
  );
}

function ReviewNote({ mark }: { mark?: ReviewMark }) {
  if (!mark) return null;
  return (
    <p className="review-note">
      読み取り結果を確認してください: {mark.reasons.map((r) => REASON_LABEL[r] ?? r).join("、")}
      {` (確信度 ${Math.round(mark.confidence * 100)}%)`}
    </p>
  );
}
