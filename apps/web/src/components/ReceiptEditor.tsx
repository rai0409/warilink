"use client";

import { useMemo, useState } from "react";
import { ChevronRight, CupSoda, Minus, Plus, TriangleAlert, Utensils } from "lucide-react";
import {
  Amount,
  Body,
  BottomBar,
  CheckRow,
  ErrorText,
  List,
  Muted,
  OutlineButton,
  Panel,
  PrimaryButton,
  Row,
  Screen,
  Segmented,
  TextButton,
  TextField,
  TopBar,
} from "@/components/kit";
import { ApiProblem, api, errorMessage, type ExpenseBody, type ExpenseDetail, type Issue, type Member } from "@/lib/api";
import { ADJUSTMENT_KIND_LABEL, ADJUSTMENT_SIGN, parseYen, yen } from "@/lib/format";
import { useExpensePreview } from "@/lib/use-preview";
import { uuid } from "@/lib/uuid";

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

const RULE_LABEL: Record<RuleType, string> = {
  proportional: "商品金額に比例",
  equal: "均等",
  members: "指定した人で均等",
};
/** いつも出す調整の欄(0 円なら送らない) */
const STANDARD_KINDS: AdjKind[] = ["tax", "discount", "service_charge"];

const newId = (p: string) => `${p}-${uuid().slice(0, 8)}`;
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
  backHref: string;
  /** 読み取り結果の確認画面などで、商品の一覧より上に出すもの(画像など) */
  header?: React.ReactNode;
  /** 編集中に別の画面で更新されたとき、最新の内容を読み直す */
  onReloadLatest?: () => void;
};

/**
 * 商品別割り勘。商品ごとに負担者を選び、税・割引などは明示したルールで配分する。
 * 金額の計算・検算はすべてサーバー(プレビュー API)が行い、ここでは入力と表示だけを扱う。
 */
export function ReceiptEditor({
  publicId, members, myMemberId, editing, initialDraft, review, sourceReceiptId, onFocusLine, onSaved, backHref, header,
  onReloadLatest,
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
  const [adjs, setAdjs] = useState<AdjDraft[]>(() => {
    const given = init?.adjustments ?? [];
    const missing = STANDARD_KINDS.filter((k) => !given.some((a) => a.kind === k)).map(
      (k): AdjDraft => ({ id: newId("a"), kind: k, label: "", amount: "0", rule: k === "service_charge" ? "equal" : "proportional", scope: "all", members: [] }),
    );
    return [...given, ...missing];
  });
  const [panel, setPanel] = useState<"assign" | "item" | "adjust" | "summary" | null>(null);
  const [editItemId, setEditItemId] = useState<string | null>(null);
  const [assignMode, setAssignMode] = useState<AllocMode>("equal");
  const [assignMembers, setAssignMembers] = useState<Set<string>>(new Set());
  const [assignValues, setAssignValues] = useState<Record<string, string>>({});
  const [conflict, setConflict] = useState(false);
  const [bulk, setBulk] = useState<Set<string>>(new Set());
  const [expenseId, setExpenseId] = useState(() => editing?.id ?? uuid());
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
    const adjustments = adjs.filter((a) => parseYen(a.amount) !== 0).map((a) => {
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
  function openAssign() {
    const first = items.find((x) => bulk.has(x.id));
    setAssignMode(bulk.size === 1 && first ? first.mode : "equal");
    // 未割当の商品は全員を選んだ状態から始める
    setAssignMembers(new Set(first?.mode === "equal" && first.members.length > 0 ? first.members : allIds));
    setAssignValues(bulk.size === 1 && first ? { ...first.values } : {});
    setPanel("assign");
  }
  function applyAssign() {
    const ids = allIds.filter((id) => assignMembers.has(id));
    setItems((xs) =>
      xs.map((x) =>
        !bulk.has(x.id)
          ? x
          : assignMode === "equal"
            ? { ...x, mode: "equal", members: ids, values: {} }
            : { ...x, mode: assignMode, members: [], values: { ...assignValues } },
      ),
    );
    setBulk(new Set());
    setPanel(null);
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
        setExpenseId(uuid());
        onSaved(saved);
      }
    } catch (e) {
      if (e instanceof ApiProblem && e.code === "REVISION_CONFLICT") setConflict(true);
      else setSaveError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const itemsSum = items.reduce((s, it) => s + (parseYen(it.line) ?? 0), 0);
  const signed = (a: AdjDraft) => {
    const v = parseYen(a.amount) ?? 0;
    return ADJUSTMENT_SIGN[a.kind] === "negative" ? -v : v;
  };
  const adjSum = adjs.reduce((s, a) => s + signed(a), 0);
  const inputSum = itemsSum + adjSum;
  const diff = total !== null ? total - inputSum : null;
  const name = (id: string) => members.find((m) => m.id === id)?.display_name ?? "";
  const selected = items.filter((x) => bulk.has(x.id));
  const editItem = items.find((x) => x.id === editItemId) ?? null;
  const kindLabel = (a: AdjDraft) => a.label || ADJUSTMENT_KIND_LABEL[a.kind] || a.kind;

  function itemSub(it: ItemDraft): string {
    const qty = `${it.qty || 1}点`;
    if (it.mode === "units") return `${qty} · 数量で配分`;
    if (it.mode === "exact") return `${qty} · 金額指定`;
    return it.members.length ? `${qty} · ${it.members.map(name).join("、")}` : `${qty} · 未割当`;
  }

  const reconciliation = (
    <div className="mt-6 border-t border-border">
      {[["商品合計", itemsSum], ["入力合計", inputSum], ["レシート合計", total ?? 0]].map(([l, v]) => (
        <p key={l as string} className="flex justify-between border-b border-border py-3 text-[14px]">
          <span className="text-muted-foreground">{l}</span>
          <span className="amount tabular-nums">{yen(v as number)}</span>
        </p>
      ))}
      {diff !== null && diff !== 0 && (
        <div role="region" aria-label="合計金額の不一致" className="border-b border-border py-3">
          <p className="flex items-center gap-2 text-[14px] font-semibold text-warning">
            <TriangleAlert className="size-5" aria-hidden />
            差額 {yen(diff)}
          </p>
          <Muted className="mt-1">金額は自動では直しません。明細を直すか、差額を調整として追加してください。</Muted>
          <TextButton onClick={() => addDifferenceLine(diff)}>差額 {yen(diff)} を調整行として追加</TextButton>
        </div>
      )}
    </div>
  );

  return (
    <Screen>
      <TopBar title="商品別" subtitle={title || undefined} back={backHref} />
      <Body>
        {header}
        <TextField label="店名・内容" value={title} onChange={setTitle} onClear={() => setTitle("")} maxLength={100}
          placeholder="金曜のごはん" />
        <TextField label="レシートの合計金額(円)" value={totalText} onChange={setTotalText} inputMode="numeric" placeholder="0"
          inputClassName="amount text-[20px] font-semibold tabular-nums" aria-invalid={totalText !== "" && total === null} />
        {totalText && total === null && <ErrorText className="my-2">合計金額は整数の円で入力してください</ErrorText>}

        <ul aria-label="商品" className="mt-6 border-t border-border">
          {items.map((it, index) => {
            const issues = issuesFor(it.id);
            const flagged = Boolean(localErrors[it.id]) || issues.length > 0;
            const label = it.name || `商品${index + 1}`;
            return (
              <li key={it.id} className="flex min-h-16 items-center gap-3 border-b border-border" onFocus={() => onFocusLine?.(it.id)}>
                <input type="checkbox" aria-label={`${label}を選択`} checked={bulk.has(it.id)}
                  onChange={() => setBulk((s) => { const n = new Set(s); if (n.has(it.id)) n.delete(it.id); else n.add(it.id); return n; })}
                  className="size-5 accent-[var(--primary)]" />
                {/(ドリンク|ビール|茶|酒|ジュース|コーヒー|ハイボール)/.test(it.name)
                  ? <CupSoda aria-hidden className="size-5 shrink-0" strokeWidth={1.6} />
                  : <Utensils aria-hidden className="size-5 shrink-0" strokeWidth={1.6} />}
                <button type="button" onClick={() => { setEditItemId(it.id); setPanel("item"); }}
                  className="flex min-h-16 min-w-0 flex-1 items-center gap-2 text-left">
                  <span className="min-w-0 flex-1">
                    <span className="block truncate text-[14px] font-medium">{label}<span className="sr-only">を編集</span></span>
                    <span className={`block truncate text-[12px] ${flagged || review?.[it.id] ? "text-warning" : "text-muted-foreground"}`}>
                      {review?.[it.id] ? "要確認 · " : ""}{itemSub(it)}
                    </span>
                  </span>
                  <span className="amount text-[16px] font-semibold tabular-nums">{parseYen(it.line) !== null ? yen(parseYen(it.line)!) : "—"}</span>
                  <ChevronRight aria-hidden className="size-4 text-muted-foreground" />
                </button>
              </li>
            );
          })}
        </ul>
        <TextButton onClick={() => { const it = emptyItem(); setItems((xs) => [...xs, it]); setEditItemId(it.id); setPanel("item"); }}>
          <Plus className="size-5" aria-hidden />
          商品を追加
        </TextButton>
        {selected.length > 0 && (
          <OutlineButton className="mt-3" onClick={openAssign}>選択した{selected.length}商品を割り当て</OutlineButton>
        )}
        <List className="mt-6">
          <Row label="税・割引・サービス料" value={yen(adjSum)} onClick={() => setPanel("adjust")} />
        </List>
        {bd && <GlobalIssues issues={bd.issues.filter((i) => !i.target && i.code !== "RECEIPT_TOTAL_MISMATCH")} />}
        {previewError && <ErrorText>{previewError}</ErrorText>}
      </Body>
      <BottomBar>
        <PrimaryButton onClick={() => setPanel("summary")}>内訳を確認</PrimaryButton>
      </BottomBar>

      {/* ---------------------------------------------------------------- 割り当て */}
      <Panel open={panel === "assign"} title="割り当て" onBack={() => setPanel(null)}
        subtitle={`${selected.map((x) => x.name || "商品").join("・")} ${yen(selected.reduce((s, x) => s + (parseYen(x.line) ?? 0), 0))}`}
        footer={<PrimaryButton disabled={assignMode === "equal" && assignMembers.size === 0} onClick={applyAssign}>{selected.length}商品に適用</PrimaryButton>}>
        <div className="mt-4">
          <Segmented label="割り当て方" value={assignMode} onChange={setAssignMode}
            options={selected.length === 1
              ? [{ value: "equal", label: "均等" }, { value: "units", label: "数量" }, { value: "exact", label: "金額指定" }]
              : [{ value: "equal", label: "均等" }]} />
          {selected.length > 1 && <Muted className="mt-2">数量・金額指定は 1 商品ずつ選んでください。</Muted>}
        </div>
        {assignMode === "equal" && (
          <ul aria-label="負担する人" className="mt-4">
            {members.map((m) => (
              <CheckRow key={m.id} label={m.display_name} checked={assignMembers.has(m.id)}
                onChange={(v) => setAssignMembers((s) => { const n = new Set(s); if (v) n.add(m.id); else n.delete(m.id); return n; })} />
            ))}
          </ul>
        )}
        {assignMode === "units" && selected[0] && (
          <ul aria-label="数量の割当" className="mt-4">
            {members.map((m) => {
              const n = Number(assignValues[m.id] ?? 0) || 0;
              const set = (v: number) => setAssignValues({ ...assignValues, [m.id]: String(Math.max(0, v)) });
              return (
                <li key={m.id} className="flex min-h-14 items-center gap-3 border-b border-border">
                  <span className="flex-1 text-[14px]">{m.display_name}</span>
                  <button type="button" aria-label={`${m.display_name}の数量を減らす`} disabled={n === 0} onClick={() => set(n - 1)}
                    className="grid size-10 place-items-center rounded-full border border-input disabled:opacity-40"><Minus className="size-4" aria-hidden /></button>
                  <output aria-label={`${m.display_name}の数量`} className="w-8 text-center tabular-nums">{n}</output>
                  <button type="button" aria-label={`${m.display_name}の数量を増やす`} onClick={() => set(n + 1)}
                    className="grid size-10 place-items-center rounded-full border border-input"><Plus className="size-4" aria-hidden /></button>
                </li>
              );
            })}
            <Muted className="mt-2">割当 {Object.values(assignValues).reduce((s, v) => s + (Number(v) || 0), 0)} / {selected[0].qty}</Muted>
          </ul>
        )}
        {assignMode === "exact" && (
          <ul aria-label="金額の指定" className="mt-4">
            {members.map((m) => (
              <li key={m.id} className="flex min-h-14 items-center gap-3 border-b border-border">
                <span className="flex-1 text-[14px]">{m.display_name}</span>
                <input aria-label={`${m.display_name}の金額(円)`} inputMode="numeric" value={assignValues[m.id] ?? ""}
                  onChange={(e) => setAssignValues({ ...assignValues, [m.id]: e.target.value })}
                  className="h-11 w-32 rounded-md border border-input px-3 text-right tabular-nums" />
              </li>
            ))}
          </ul>
        )}
      </Panel>

      {/* ---------------------------------------------------------------- 商品の編集 */}
      <Panel open={panel === "item" && editItem !== null} title="商品" onBack={() => setPanel(null)}
        footer={<PrimaryButton onClick={() => setPanel(null)}>完了</PrimaryButton>}>
        {editItem && (
          <div role="group" aria-label="商品の内容">
            <ReviewNote mark={review?.[editItem.id]} />
            <TextField label="商品名" value={editItem.name} maxLength={100} onChange={(v) => patchItem(editItem.id, { name: v })} />
            <div className="grid grid-cols-2 gap-3">
              <TextField label="数量" value={editItem.qty} inputMode="numeric" onChange={(v) => patchItem(editItem.id, { qty: v })} />
              <TextField label="金額(円)" value={editItem.line} inputMode="numeric" onChange={(v) => patchItem(editItem.id, { line: v })} />
            </div>
            <label className="mt-6 block text-[13px] text-muted-foreground">
              税率
              <select value={editItem.tax} onChange={(e) => patchItem(editItem.id, { tax: e.target.value as TaxGroup })}
                className="mt-2 h-12 w-full rounded-md border border-input bg-background px-3 text-[15px] text-foreground">
                <option value="">指定なし</option>
                <option value="8">8%(軽減)</option>
                <option value="10">10%</option>
              </select>
            </label>
            {localErrors[editItem.id] && <ErrorText>{localErrors[editItem.id]}</ErrorText>}
            {issuesFor(editItem.id).filter((i) => i.code !== "ITEM_UNALLOCATED").map((i, n) => <ErrorText key={n}>{i.message}</ErrorText>)}
            <TextButton tone="danger" className="mt-6" disabled={items.length === 1}
              onClick={() => { setItems((xs) => xs.filter((x) => x.id !== editItem.id)); setBulk((s) => { const n = new Set(s); n.delete(editItem.id); return n; }); setPanel(null); }}>
              この商品を削除
            </TextButton>
          </div>
        )}
      </Panel>

      {/* ---------------------------------------------------------------- 税・割引・サービス料 */}
      <Panel open={panel === "adjust"} title="税・割引・サービス料" onBack={() => setPanel(null)}
        footer={<PrimaryButton onClick={() => setPanel(null)}>適用する</PrimaryButton>}>
        <Muted className="mt-2">内税のレシートでは税を入れないでください(商品の金額に含まれているため二重計上になります)。</Muted>
        {adjs.map((a) => {
          const label = kindLabel(a);
          const sign = ADJUSTMENT_SIGN[a.kind];
          return (
            <div key={a.id} role="group" aria-label={label} className="border-b border-border py-4">
              <ReviewNote mark={review?.[a.id]} />
              <div className="flex items-center gap-3">
                <span className="flex-1 text-[15px] font-semibold">{label}</span>
                <input aria-label={`${label}の金額(円)${sign === "negative" ? "(差し引く額)" : ""}`} inputMode="numeric" value={a.amount}
                  onChange={(e) => patchAdj(a.id, { amount: e.target.value })}
                  className="h-11 w-36 rounded-md border border-input px-3 text-right text-[15px] tabular-nums" />
              </div>
              <label className="mt-3 flex items-center gap-3 text-[13px] text-muted-foreground">
                <span className="flex-1">配分方法</span>
                <select aria-label={`${label}の配分方法`} value={a.rule} onChange={(e) => patchAdj(a.id, { rule: e.target.value as RuleType })}
                  className="h-11 w-44 rounded-md border border-input bg-background px-3 text-[14px] text-foreground">
                  {(Object.keys(RULE_LABEL) as RuleType[]).map((r) => <option key={r} value={r}>{RULE_LABEL[r]}</option>)}
                </select>
              </label>
              {a.rule !== "members" && (a.kind === "tax" || a.scope !== "all") && (
                <label className="mt-2 flex items-center gap-3 text-[13px] text-muted-foreground">
                  <span className="flex-1">対象の商品</span>
                  <select aria-label={`${label}の対象`} value={a.scope} onChange={(e) => patchAdj(a.id, { scope: e.target.value as AdjDraft["scope"] })}
                    className="h-11 w-44 rounded-md border border-input bg-background px-3 text-[14px] text-foreground">
                    <option value="all">すべての商品</option>
                    <option value="8">税率8%の商品</option>
                    <option value="10">税率10%の商品</option>
                    {a.scopeItemIds?.length ? <option value="items">{items.filter((x) => a.scopeItemIds!.includes(x.id)).map((x) => x.name || "商品").join("・")}のみ</option> : null}
                  </select>
                </label>
              )}
              {a.rule === "members" && (
                <ul aria-label={`${label}を負担する人`} className="mt-2">
                  {members.map((m) => (
                    <CheckRow key={m.id} label={m.display_name} checked={a.members.includes(m.id)}
                      onChange={() => patchAdj(a.id, { members: toggle(a.members, m.id) })} />
                  ))}
                </ul>
              )}
              {localErrors[a.id] && <ErrorText>{localErrors[a.id]}</ErrorText>}
              {issuesFor(a.id).map((i, n) => <ErrorText key={n}>{i.message}</ErrorText>)}
              {!STANDARD_KINDS.includes(a.kind) && (
                <TextButton tone="danger" onClick={() => setAdjs((xs) => xs.filter((x) => x.id !== a.id))}>この調整を削除</TextButton>
              )}
            </div>
          );
        })}
        {reconciliation}
      </Panel>

      {/* ---------------------------------------------------------------- 内訳の確認と登録 */}
      <Panel open={panel === "summary"} title="内訳を確認" onBack={() => setPanel(null)}
        footer={conflict ? (
          <div role="alert" className="text-center">
            <p className="text-[14px] font-semibold">編集中に更新がありました</p>
            <p className="mt-1 text-[12px] text-muted-foreground">最新の内容を確認してください。</p>
            <PrimaryButton className="mt-4" onClick={() => onReloadLatest?.()}>最新の内容を確認</PrimaryButton>
            <TextButton className="w-full justify-center" onClick={() => setConflict(false)}>編集中の内容</TextButton>
          </div>
        ) : (
          <PrimaryButton disabled={!bd?.ok || pending || busy || !body} onClick={save}>
            {editing ? "変更を保存" : "登録する"}
          </PrimaryButton>
        )}>
        <Amount label="レシート合計" value={total !== null ? yen(total) : "—"} />
        <label className="mt-6 flex items-center gap-3 text-[13px] text-muted-foreground">
          <span className="flex-1">支払った人</span>
          <select value={payer} onChange={(e) => setPayer(e.target.value)} aria-label="支払った人"
            className="h-11 w-44 rounded-md border border-input bg-background px-3 text-[14px] text-foreground">
            {members.map((m) => <option key={m.id} value={m.id}>{m.display_name}</option>)}
          </select>
        </label>
        {reconciliation}
        {bd?.ok && (
          <>
            <p className="mt-6 text-[14px] font-semibold text-success">明細の合計がレシートの合計と一致しました。</p>
            <ul aria-label="メンバー別の負担" className="mt-2 text-[14px]">
              {members.filter((m) => bd.shares[m.id] !== undefined).map((m) => (
                <li key={m.id} className="flex justify-between border-b border-border py-3">
                  <span>{m.display_name}</span>
                  <span className="amount font-semibold tabular-nums">{yen(bd.shares[m.id])}</span>
                </li>
              ))}
            </ul>
          </>
        )}
        {bd && <GlobalIssues issues={bd.issues.filter((i) => i.code !== "RECEIPT_TOTAL_MISMATCH")} />}
        {!bd?.ok && <Muted className="mt-4">すべての商品に負担する人を選び、合計が一致すると登録できます。</Muted>}
        {saveError && <ErrorText>{saveError}</ErrorText>}
      </Panel>
    </Screen>
  );
}

function GlobalIssues({ issues }: { issues: Issue[] }) {
  if (issues.length === 0) return null;
  return (
    <ul aria-label="確認が必要な点" className="mt-4 space-y-1 text-[13px] text-destructive">
      {issues.map((i, n) => <li key={n}>{i.message}</li>)}
    </ul>
  );
}

function ReviewNote({ mark }: { mark?: ReviewMark }) {
  if (!mark) return null;
  return (
    <p className="mt-4 text-[13px] text-warning">
      読み取り結果を確認してください: {mark.reasons.map((r) => REASON_LABEL[r] ?? r).join("、")}
      {` (確信度 ${Math.round(mark.confidence * 100)}%)`}
    </p>
  );
}
