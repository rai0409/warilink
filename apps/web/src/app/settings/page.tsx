"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { Clock, CreditCard, FileText, KeyRound, ShieldCheck, Tag, User } from "lucide-react";
import { Body, ConfirmDialog, ErrorText, List, Muted, PrimaryButton, Row, Screen, Sheet, StatusText, TextField, TopBar } from "@/components/kit";
import { api, errorMessage, type Me, type Plans } from "@/lib/api";
import { loadDefaultName, saveDefaultName } from "@/lib/prefs";
import { IdentityChanged, deleteAccount } from "@/lib/stepup";

/** アプリ全体の設定(グループの設定とは別) */
export default function SettingsPage() {
  const router = useRouter();
  const [me, setMe] = useState<Me | null | undefined>(undefined);
  const [plans, setPlans] = useState<Plans | null>(null);
  const [name, setName] = useState("");
  const [draft, setDraft] = useState("");
  const [editName, setEditName] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    setName(loadDefaultName());
    api<Me | null>("/identity/me").then(setMe).catch(() => setMe(null));
    api<Plans>("/plans").then(setPlans).catch(() => setPlans(null));
  }, []);

  async function remove() {
    setBusy(true);
    setError(null);
    try {
      await deleteAccount();
      router.push("/?deleted=1");
    } catch (e) {
      setConfirmDelete(false);
      if (e instanceof IdentityChanged) setError("別のアカウントの Passkey で確認されたため、退会を中止しました。");
      else setError(e instanceof Error && e.name === "NotAllowedError" ? "本人確認が取り消されました" : errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const registered = me?.kind === "registered";
  const showAccount = registered || plans?.passkeys_available || plans?.payment_destinations_available;

  return (
    <Screen>
      <TopBar title="設定" back="/" />
      <Body>
        <Muted className="mt-4">{registered ? "Passkey でログイン中" : "ゲストとして利用中"}</Muted>
        <List aria-label="設定" className="mt-3 border-t border-border">
          <Row icon={<User />} label="名前" sub={name || "未設定"} onClick={() => { setDraft(name); setEditName(true); }} />
          {showAccount && <Row icon={<KeyRound />} label="アカウントと Passkey" href="/account" />}
          {plans?.payment_destinations_available && registered && <Row icon={<CreditCard />} label="受取口座" href="/account#dest-h" />}
          <Row icon={<Clock />} label="データの保存期間" href="/settings/retention" />
          <Row icon={<FileText />} label="利用規約" href="/settings/terms" />
          <Row icon={<ShieldCheck />} label="プライバシーポリシー" href="/settings/privacy" />
          <Row icon={<Tag />} label="料金プラン" href="/pricing" />
        </List>
        {me && (
          <List className="mt-12 border-t border-border">
            <Row label="退会する" tone="danger" onClick={() => setConfirmDelete(true)} />
          </List>
        )}
        {message && <StatusText>{message}</StatusText>}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>

      <Sheet open={editName} onOpenChange={setEditName} title="名前"
        footer={
          <PrimaryButton disabled={!draft.trim()} onClick={() => {
            saveDefaultName(draft.trim());
            setName(draft.trim());
            setEditName(false);
            setMessage("名前を保存しました");
          }}>保存する</PrimaryButton>
        }>
        <TextField label="グループを作るときや参加するときに最初に入る名前" value={draft} onChange={setDraft}
          onClear={() => setDraft("")} maxLength={40} />
        <Muted className="mt-2 text-[12px]">この端末にだけ保存します。参加中のグループでの名前は、各グループのメンバー画面で変えられます。</Muted>
      </Sheet>

      <ConfirmDialog open={confirmDelete} onOpenChange={setConfirmDelete} title="退会しますか?"
        description={registered
          ? "元に戻せません。グループには名前と過去の記録が残ります。削除の前に Passkey でもう一度確認します。"
          : "元に戻せません。グループには名前と過去の記録が残ります。"}
        confirmLabel="退会する" busy={busy} danger onConfirm={remove} />
    </Screen>
  );
}
