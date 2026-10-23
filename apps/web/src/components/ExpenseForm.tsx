"use client";

import { useMemo, useState } from "react";
import { ApiProblem, api, errorMessage, type ExpenseBody, type ExpenseDetail, type Member } from "@/lib/api";
import { formatBp, formatYen, parsePercentBp, parseYen } from "@/lib/format";
import { useExpensePreview } from "@/lib/use-preview";
import { IssueList, PreviewShares } from "@/components/PreviewShares";

type Mode = "equal" | "weight" | "percentage" | "exact";

const MODE_LABEL: Record<Mode, string> = {
  equal: "均等",
  weight: "比率",
  percentage: "割合(%)",
  exact: "金額指定",
};

type Props = {
  publicId: string;
  members: Member[];
  myMemberId: string;
  /** 指定すると編集(PUT + If-Match)。itemized 以外の支出のみ。 */
  editing?: ExpenseDetail;
  onSaved: (expenseId: string) => void;
};

type Spec = {
  title: string;
  amount_minor: number;
  payers: { member_id: string; amount_minor: number }[];
  split: { mode: Mode; shares: { member_id: string; value: number }[] };
};

function initialState(editing: ExpenseDetail | undefined, members: Member[], me: string) {
  const spec = editing?.spec as Spec | undefined;
  const mode = spec?.split.mode ?? "equal";
  const values: Record<string, string> = {};
  for (const s of spec?.split.shares ?? []) {
    values[s.member_id] = mode === "percentage" ? formatBp(s.value) : String(s.value);
  }
  const payerValues: Record<string, string> = {};
  for (const p of spec?.payers ?? []) payerValues[p.member_id] = String(p.amount_minor);
  return {
    title: spec?.title ?? "",
    amountText: spec ? String(spec.amount_minor) : "",
    payer: spec?.payers[0]?.member_id ?? me,
    multiPayer: (spec?.payers.length ?? 1) > 1,
    payerValues,
    mode,
    selected: new Set(spec ? spec.split.shares.map((s) => s.member_id) : members.map((m) => m.id)),
    values,
  };
}

/** 支出の入力(均等・比率・割合・金額指定)。負担額は入力のたびにサーバーのプレビュー API で計算する。 */
export function ExpenseForm({ publicId, members, myMemberId, editing, onSaved }: Props) {
  const init = useMemo(() => initialState(editing, members, myMemberId), [editing, members, myMemberId]);
  const [title, setTitle] = useState(init.title);
  const [amountText, setAmountText] = useState(init.amountText);
  const [payer, setPayer] = useState(init.payer);
  const [multiPayer, setMultiPayer] = useState(init.multiPayer);
  const [payerValues, setPayerValues] = useState<Record<string, string>>(init.payerValues);
  const [mode, setMode] = useState<Mode>(init.mode);
  const [selected, setSelected] = useState<Set<string>>(init.selected);
  const [values, setValues] = useState<Record<string, string>>(init.values);
  const [expenseId, setExpenseId] = useState(() => editing?.id ?? crypto.randomUUID());
  const [saveError, setSaveError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const amount = parseYen(amountText);

  const body = useMemo((): ExpenseBody | null => {
    if (amount === null || !title.trim()) return null;
    const ids = members.map((m) => m.id).filter((id) => selected.has(id));
    const shares = ids.map((id) => {
      if (mode === "equal") return { member_id: id, value: 1 };
      const raw = values[id] ?? "";
      const v = mode === "percentage" ? parsePercentBp(raw) : parseYen(raw);
      return { member_id: id, value: v ?? -1 };
    });
    let payers = [{ member_id: payer, amount_minor: amount }];
    if (multiPayer) {
      payers = members
        .map((m) => ({ member_id: m.id, amount_minor: parseYen(payerValues[m.id] ?? "") ?? 0 }))
        .filter((p) => p.amount_minor !== 0);
      if (payers.length === 0) return null;
    }
    return { title: title.trim(), amount_minor: amount, payers, split: { mode, shares } };
  }, [amount, title, members, selected, mode, values, payer, multiPayer, payerValues]);

  const { preview, error: previewError, pending } = useExpensePreview(publicId, body, expenseId);

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
        const fresh = initialState(undefined, members, myMemberId);
        setTitle(fresh.title);
        setAmountText(fresh.amountText);
        setValues({});
        setPayerValues({});
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

  const bd = preview?.breakdown;
  const unit = mode === "weight" ? "比率" : mode === "percentage" ? "割合(%)" : "金額(円)";
  return (
    <section className="card" aria-labelledby="expense-form">
      <h2 id="expense-form">{editing ? "支出を編集" : "支出を追加"}</h2>
      <label htmlFor="e-title">内容</label>
      <input id="e-title" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={100} />
      <label htmlFor="e-amount">金額(円)</label>
      <input id="e-amount" inputMode="numeric" value={amountText} onChange={(e) => setAmountText(e.target.value)} />
      {amountText && amount === null && <p className="error">金額は整数の円で入力してください</p>}

      <fieldset>
        <legend>立て替えた人</legend>
        {!multiPayer ? (
          <select aria-label="立て替えた人" value={payer} onChange={(e) => setPayer(e.target.value)}>
            {members.map((m) => (
              <option key={m.id} value={m.id}>{m.display_name}</option>
            ))}
          </select>
        ) : (
          members.map((m) => (
            <div key={m.id} className="inline-field">
              <label htmlFor={`p-${m.id}`}>{m.display_name}の立替額(円)</label>
              <input
                id={`p-${m.id}`}
                inputMode="numeric"
                value={payerValues[m.id] ?? ""}
                onChange={(e) => setPayerValues({ ...payerValues, [m.id]: e.target.value })}
              />
            </div>
          ))
        )}
        <label className="check">
          <input type="checkbox" checked={multiPayer} onChange={(e) => setMultiPayer(e.target.checked)} />
          複数人で立て替えた
        </label>
      </fieldset>

      <fieldset>
        <legend>分け方</legend>
        <div className="row" role="group" aria-label="分け方">
          {(Object.keys(MODE_LABEL) as Mode[]).map((m) => (
            <button key={m} type="button" className="toggle" aria-pressed={mode === m} onClick={() => setMode(m)}>
              {MODE_LABEL[m]}
            </button>
          ))}
        </div>
      </fieldset>

      <fieldset>
        <legend>負担する人</legend>
        <div className="row" role="group" aria-label="負担する人">
          <button type="button" className="toggle" onClick={() => setSelected(new Set(members.map((m) => m.id)))}>
            全員
          </button>
          {members.map((m) => (
            <button
              key={m.id}
              type="button"
              className="toggle"
              aria-pressed={selected.has(m.id)}
              onClick={() => {
                const next = new Set(selected);
                if (next.has(m.id)) next.delete(m.id);
                else next.add(m.id);
                setSelected(next);
              }}
            >
              {m.display_name}
            </button>
          ))}
        </div>
        {mode !== "equal" &&
          members
            .filter((m) => selected.has(m.id))
            .map((m) => (
              <div key={m.id} className="inline-field">
                <label htmlFor={`v-${m.id}`}>{m.display_name}の{unit}</label>
                <input
                  id={`v-${m.id}`}
                  inputMode="decimal"
                  value={values[m.id] ?? ""}
                  onChange={(e) => setValues({ ...values, [m.id]: e.target.value })}
                />
              </div>
            ))}
      </fieldset>

      {bd && (
        <div aria-live="polite" className="preview">
          <IssueList issues={bd.issues} />
          {bd.ok && <PreviewShares members={members} shares={bd.shares} />}
          {preview?.projected_settlement && (
            <p className="muted">
              登録後の精算: {preview.projected_settlement.transfers.length}件(
              {preview.projected_settlement.optimization.display_label})
            </p>
          )}
        </div>
      )}
      {previewError && <p className="error" role="alert">{previewError}</p>}
      {saveError && <p className="error" role="alert">{saveError}</p>}
      <p className="row actions">
        <button type="button" disabled={!bd?.ok || pending || busy} onClick={save}>
          {editing ? "変更を保存" : "この内容で登録"}
        </button>
      </p>
      {editing && <p className="muted">保存すると変更前の内容は履歴に残ります。</p>}
      {amount !== null && bd?.ok && <p className="muted">合計 {formatYen(amount)} をサーバーで検算済みです。</p>}
    </section>
  );
}
