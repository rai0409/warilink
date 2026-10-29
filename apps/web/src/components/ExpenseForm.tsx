"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import { ChartColumn, ChartPie, CircleCheck, CircleDollarSign, Plus, ShoppingBag, UsersRound, X } from "lucide-react";
import {
  Body,
  BottomBar,
  CheckRow,
  ErrorText,
  List,
  Muted,
  Panel,
  PrimaryButton,
  RadioRow,
  Row,
  Screen,
  Segmented,
  Sheet,
  TextButton,
  TextField,
  TopBar,
} from "@/components/kit";
import { ApiProblem, api, errorMessage, type ExpenseBody, type ExpenseDetail, type Member } from "@/lib/api";
import { CATEGORY_LABEL, formatBp, parsePercentBp, parseYen, yen } from "@/lib/format";
import { useExpensePreview } from "@/lib/use-preview";

type Mode = "equal" | "weight" | "percentage" | "exact";

const MODES: { value: Mode | "itemized"; label: string; sub: string; icon: React.ReactNode }[] = [
  { value: "equal", label: "均等", sub: "同じ金額ずつ", icon: <UsersRound strokeWidth={1.6} /> },
  { value: "weight", label: "比率", sub: "2 : 1 : 1", icon: <ChartColumn strokeWidth={1.6} /> },
  { value: "percentage", label: "割合", sub: "50%・30%・20%", icon: <ChartPie strokeWidth={1.6} /> },
  { value: "exact", label: "金額指定", sub: "一人ずつ入力", icon: <CircleDollarSign strokeWidth={1.6} /> },
  { value: "itemized", label: "商品別", sub: "商品ごとに選ぶ", icon: <ShoppingBag strokeWidth={1.6} /> },
];
const MODE_LABEL: Record<Mode, string> = { equal: "均等", weight: "比率", percentage: "割合", exact: "金額指定" };
const UNIT_LABEL: Record<Exclude<Mode, "equal">, string> = { weight: "比率", percentage: "割合(%)", exact: "金額(円)" };

type Props = {
  publicId: string;
  members: Member[];
  myMemberId: string;
  /** 指定すると編集(PUT + If-Match)。itemized 以外の支出のみ。 */
  editing?: ExpenseDetail;
  onSaved: (expenseId: string) => void;
  backHref: string;
  /** 編集中に別の画面で更新されたとき、最新の内容を読み直す */
  onReloadLatest?: () => void;
};

type Spec = {
  title: string;
  amount_minor: number;
  category?: string | null;
  occurred_on?: string | null;
  payers: { member_id: string; amount_minor: number }[];
  split: { mode: Mode; shares: { member_id: string; value: number }[] };
};

type PayerDraft = { member_id: string; amount: string };

function initialState(editing: ExpenseDetail | undefined, members: Member[], me: string) {
  const spec = editing?.spec as Spec | undefined;
  const mode = spec?.split.mode ?? "equal";
  const values: Record<string, string> = {};
  for (const s of spec?.split.shares ?? []) {
    values[s.member_id] = mode === "percentage" ? formatBp(s.value) : String(s.value);
  }
  const payers: PayerDraft[] = spec
    ? spec.payers.map((p) => ({ member_id: p.member_id, amount: String(p.amount_minor) }))
    : [{ member_id: me, amount: "" }];
  return {
    title: spec?.title ?? "",
    category: spec?.category ?? "",
    occurredOn: spec?.occurred_on ?? "",
    amountText: spec ? String(spec.amount_minor) : "",
    payers,
    mode,
    selected: new Set(spec ? spec.split.shares.map((s) => s.member_id) : members.map((m) => m.id)),
    values,
  };
}

/**
 * 支出の入力(均等・比率・割合・金額指定)。分け方・負担額・立て替えた人はそれぞれの画面で決める。
 * 負担額は入力のたびにサーバーのプレビュー API で計算する(画面では金額を計算しない)。
 */
export function ExpenseForm({ publicId, members, myMemberId, editing, onSaved, backHref, onReloadLatest }: Props) {
  const router = useRouter();
  const init = useMemo(() => initialState(editing, members, myMemberId), [editing, members, myMemberId]);
  const [title, setTitle] = useState(init.title);
  const [category, setCategory] = useState(init.category);
  const [occurredOn, setOccurredOn] = useState(init.occurredOn);
  const [amountText, setAmountText] = useState(init.amountText);
  const [payers, setPayers] = useState<PayerDraft[]>(init.payers);
  const [mode, setMode] = useState<Mode>(init.mode);
  const [selected, setSelected] = useState<Set<string>>(init.selected);
  const [values, setValues] = useState<Record<string, string>>(init.values);
  const [expenseId, setExpenseId] = useState(() => editing?.id ?? crypto.randomUUID());
  const [panel, setPanel] = useState<"mode" | "shares" | "payers" | null>(null);
  const [pick, setPick] = useState<"add" | "replace" | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [conflict, setConflict] = useState(false);
  const [busy, setBusy] = useState(false);

  const amount = parseYen(amountText);
  const name = (id: string) => members.find((m) => m.id === id)?.display_name ?? "";
  const multiPayer = payers.length > 1;
  const payerSum = payers.reduce((s, p) => s + (parseYen(p.amount) ?? 0), 0);

  const body = useMemo((): ExpenseBody | null => {
    if (amount === null || !title.trim()) return null;
    const ids = members.map((m) => m.id).filter((id) => selected.has(id));
    const shares = ids.map((id) => {
      if (mode === "equal") return { member_id: id, value: 1 };
      const raw = values[id] ?? "";
      const v = mode === "percentage" ? parsePercentBp(raw) : parseYen(raw);
      return { member_id: id, value: v ?? -1 };
    });
    const ps = multiPayer
      ? payers.map((p) => ({ member_id: p.member_id, amount_minor: parseYen(p.amount) ?? 0 })).filter((p) => p.amount_minor !== 0)
      : [{ member_id: payers[0]?.member_id ?? myMemberId, amount_minor: amount }];
    if (ps.length === 0) return null;
    return {
      title: title.trim(),
      amount_minor: amount,
      payers: ps,
      split: { mode, shares },
      category: (category || null) as ExpenseBody["category"],
      occurred_on: occurredOn || null,
    };
  }, [amount, title, members, selected, mode, values, payers, multiPayer, myMemberId, category, occurredOn]);

  const { preview, error: previewError, pending } = useExpensePreview(publicId, body, expenseId);
  const bd = preview?.breakdown;

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
          body: { ...body, client_expense_id: expenseId },
        });
        const saved = expenseId;
        setExpenseId(crypto.randomUUID());
        onSaved(saved);
      }
    } catch (e) {
      if (e instanceof ApiProblem && e.code === "REVISION_CONFLICT") setConflict(true);
      else setSaveError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  function choosePayer(id: string) {
    if (pick === "replace") setPayers([{ member_id: id, amount: "" }]);
    else {
      // 2 人目を足すときは、1 人目の立替額を支出の合計で埋めておく(あとで直せる)
      const first = payers.length === 1 ? [{ ...payers[0], amount: payers[0].amount || (amount !== null ? String(amount) : "") }] : payers;
      setPayers([...first, { member_id: id, amount: "" }]);
    }
    setPick(null);
  }

  const sharesSummary = mode === "equal" ? `${selected.size}人で均等` : `${selected.size}人 · ${MODE_LABEL[mode]}`;
  const payerSummary = payers.map((p) => name(p.member_id)).join("・");
  const totalRow = (
    <p className="mt-4 flex justify-between text-[13px] text-muted-foreground">
      <span>支出の合計</span>
      <span className="amount text-[16px] font-semibold text-foreground tabular-nums">{amount !== null ? yen(amount) : "—"}</span>
    </p>
  );

  return (
    <Screen>
      <TopBar title={editing ? "支出を編集" : "支出を追加"} back={backHref} />
      <Body>
        <TextField label="内容" value={title} onChange={setTitle} onClear={() => setTitle("")} maxLength={100} placeholder="夕食" />
        <TextField label="金額(円)" value={amountText} onChange={setAmountText} inputMode="numeric" placeholder="0"
          inputClassName="amount text-[20px] font-semibold tabular-nums" aria-invalid={amountText !== "" && amount === null} />
        {amountText && amount === null && <ErrorText className="my-2">金額は整数の円で入力してください</ErrorText>}
        <div className="mt-6 grid grid-cols-2 gap-3">
          <label className="block text-[13px] text-muted-foreground">
            カテゴリ(任意)
            <select value={category} onChange={(e) => setCategory(e.target.value)}
              className="mt-2 h-12 w-full rounded-md border border-input bg-background px-3 text-[15px] text-foreground">
              <option value="">未分類</option>
              {Object.entries(CATEGORY_LABEL).map(([v, l]) => (
                <option key={v} value={v}>{l}</option>
              ))}
            </select>
          </label>
          <label className="block text-[13px] text-muted-foreground">
            日付(任意)
            <input type="date" value={occurredOn} onChange={(e) => setOccurredOn(e.target.value)}
              className="mt-2 h-12 w-full rounded-md border border-input bg-background px-3 text-[15px] text-foreground" />
          </label>
        </div>
        <List className="mt-8">
          <Row label="分け方" value={MODE_LABEL[mode]} onClick={() => setPanel("mode")} />
          <Row label="負担額" value={sharesSummary} onClick={() => setPanel("shares")} />
          <Row label="立て替えた人" value={payerSummary} onClick={() => setPanel("payers")} />
        </List>
        {bd && (
          <div aria-live="polite" className="mt-6">
            {bd.issues.length > 0 && (
              <ul aria-label="確認が必要な点" className="space-y-1 text-[13px] text-destructive">
                {bd.issues.map((i, n) => (
                  <li key={n}>{i.message}</li>
                ))}
              </ul>
            )}
            {bd.ok && (
              <ul aria-label="負担額のプレビュー" className="text-[14px]">
                {members.filter((m) => bd.shares[m.id] !== undefined).map((m) => (
                  <li key={m.id} className="flex justify-between py-1.5">
                    <span>{m.display_name}</span>
                    <span className="amount tabular-nums">{yen(bd.shares[m.id])}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
        {previewError && <ErrorText>{previewError}</ErrorText>}
        {saveError && <ErrorText>{saveError}</ErrorText>}
        {editing && <Muted className="mt-4">保存すると変更前の内容は履歴に残ります。</Muted>}
      </Body>
      <BottomBar>
        {conflict ? (
          <div role="alert" className="border-t border-border pt-4 text-center">
            <p className="text-[14px] font-semibold">編集中に更新がありました</p>
            <p className="mt-1 text-[12px] text-muted-foreground">最新の内容を確認してください。</p>
            <PrimaryButton className="mt-4" onClick={() => onReloadLatest?.()}>最新の内容を確認</PrimaryButton>
            <TextButton className="w-full justify-center" onClick={() => setConflict(false)}>編集中の内容</TextButton>
          </div>
        ) : (
          <PrimaryButton disabled={!bd?.ok || pending || busy} onClick={save}>
            {editing ? "変更を保存" : "この内容で登録"}
          </PrimaryButton>
        )}
      </BottomBar>

      {/* ---------------------------------------------------------------- 分け方 */}
      <Panel open={panel === "mode"} title="分け方" onBack={() => setPanel(null)}
        footer={<PrimaryButton onClick={() => setPanel(null)}>適用する</PrimaryButton>}>
        <fieldset className="mt-4">
          <legend className="sr-only">分け方</legend>
          <ul>
            {MODES.map((m) => (
              <RadioRow key={m.value} name="mode" icon={m.icon} label={m.label} sub={m.sub}
                checked={m.value === mode}
                disabled={m.value === "itemized" && Boolean(editing)}
                onChange={() => {
                  if (m.value === "itemized") router.push(`/g/${publicId}/receipt`);
                  else setMode(m.value);
                }} />
            ))}
          </ul>
        </fieldset>
      </Panel>

      {/* ---------------------------------------------------------------- 負担額 */}
      <Panel open={panel === "shares"} title="負担額" onBack={() => setPanel(null)}
        footer={<PrimaryButton onClick={() => setPanel(null)}>適用する</PrimaryButton>}>
        {totalRow}
        {(mode === "weight" || mode === "percentage") && (
          <div className="mt-4">
            <Segmented label="比率と割合の切り替え" value={mode} onChange={setMode}
              options={[{ value: "weight", label: "比率" }, { value: "percentage", label: "割合" }]} />
          </div>
        )}
        {mode === "equal" ? (
          <ul aria-label="負担する人" className="mt-4">
            {members.map((m) => (
              <CheckRow key={m.id} checked={selected.has(m.id)} label={m.display_name}
                onChange={(v) => {
                  const next = new Set(selected);
                  if (v) next.add(m.id);
                  else next.delete(m.id);
                  setSelected(next);
                }}
                right={bd?.ok && bd.shares[m.id] !== undefined ? (
                  <span className="amount text-[16px] font-semibold tabular-nums">{yen(bd.shares[m.id])}</span>
                ) : undefined} />
            ))}
          </ul>
        ) : (
          <table className="mt-4 w-full text-[14px]">
            <thead>
              <tr className="border-b border-border text-[12px] text-muted-foreground">
                <th scope="col" className="py-2 text-left font-normal">名前</th>
                <th scope="col" className="py-2 text-center font-normal">{UNIT_LABEL[mode]}</th>
                <th scope="col" className="py-2 text-right font-normal">負担額</th>
              </tr>
            </thead>
            <tbody>
              {members.map((m) => (
                <tr key={m.id} className="border-b border-border">
                  <th scope="row" className="py-3 text-left font-normal">{m.display_name}</th>
                  <td className="py-2 text-center">
                    <input
                      aria-label={`${m.display_name}の${UNIT_LABEL[mode]}`}
                      inputMode="decimal"
                      value={selected.has(m.id) ? (values[m.id] ?? "") : ""}
                      onChange={(e) => {
                        const v = e.target.value;
                        setValues({ ...values, [m.id]: v });
                        const next = new Set(selected);
                        if (v.trim() === "" || v.trim() === "0") next.delete(m.id);
                        else next.add(m.id);
                        setSelected(next);
                      }}
                      className="h-10 w-20 rounded-md border border-input text-center tabular-nums"
                    />
                  </td>
                  <td className="amount py-3 text-right font-semibold tabular-nums">
                    {bd?.ok && bd.shares[m.id] !== undefined ? yen(bd.shares[m.id]) : ""}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        {bd && !bd.ok && bd.issues[0] && <ErrorText>{bd.issues[0].message}</ErrorText>}
        {bd?.ok && (
          <p className="mt-4 flex justify-between text-[14px]">
            <span>合計</span>
            <span className="amount font-semibold tabular-nums">{amount !== null ? yen(amount) : ""}</span>
          </p>
        )}
      </Panel>

      {/* ---------------------------------------------------------------- 立て替えた人 */}
      <Panel open={panel === "payers"} title="立て替えた人" onBack={() => setPanel(null)}
        footer={<PrimaryButton disabled={multiPayer && payerSum !== amount} onClick={() => setPanel(null)}>適用する</PrimaryButton>}>
        {totalRow}
        <ul aria-label="立て替えた人の一覧" className="mt-4 border-t border-border">
          {payers.map((p, i) => (
            <li key={p.member_id} className="flex min-h-16 items-center gap-3 border-b border-border">
              {multiPayer ? (
                <>
                  <span className="flex-1 text-[14px]">{name(p.member_id)}</span>
                  <input aria-label={`${name(p.member_id)}の立替額(円)`} inputMode="numeric" value={p.amount}
                    onChange={(e) => setPayers(payers.map((x, j) => (j === i ? { ...x, amount: e.target.value } : x)))}
                    className="h-11 w-32 rounded-md border border-input px-3 text-right tabular-nums" />
                  <button type="button" aria-label={`${name(p.member_id)}を外す`}
                    onClick={() => setPayers(payers.filter((_, j) => j !== i))}
                    className="grid size-10 place-items-center text-muted-foreground">
                    <X className="size-5" aria-hidden />
                  </button>
                </>
              ) : (
                <button type="button" onClick={() => setPick("replace")} aria-label={`立て替えた人: ${name(p.member_id)}(変更する)`}
                  className="flex min-h-16 flex-1 items-center justify-between text-left">
                  <span className="text-[14px]">{name(p.member_id)}</span>
                  <span className="amount text-[16px] font-semibold tabular-nums">{amount !== null ? yen(amount) : ""}</span>
                </button>
              )}
            </li>
          ))}
        </ul>
        <div className="border-b border-border text-center">
          <TextButton onClick={() => setPick("add")} disabled={payers.length >= members.length}>
            <Plus className="size-5" aria-hidden />
            立て替えた人を追加
          </TextButton>
        </div>
        {multiPayer && (
          <p className="mt-4 flex items-center justify-between text-[14px]">
            <span>合計</span>
            <span className="flex items-center gap-2">
              <span className="amount text-[16px] font-semibold tabular-nums">{yen(payerSum)}</span>
              {payerSum === amount && <CircleCheck className="size-5 text-success" aria-label="支出の合計と一致" />}
            </span>
          </p>
        )}
        {multiPayer && amount !== null && payerSum !== amount && (
          <ErrorText>立て替えた額の合計を {yen(amount)} にしてください</ErrorText>
        )}
      </Panel>

      <Sheet open={pick !== null} onOpenChange={(v) => !v && setPick(null)}
        title={pick === "replace" ? "立て替えた人を選ぶ" : "立て替えた人を追加"}>
        <ul className="mt-2">
          {members.filter((m) => pick === "replace" || !payers.some((p) => p.member_id === m.id)).map((m) => (
            <Row key={m.id} label={m.display_name} onClick={() => choosePayer(m.id)} />
          ))}
        </ul>
      </Sheet>
    </Screen>
  );
}
