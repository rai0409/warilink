"use client";

import { useState } from "react";
import { Link2, Plus } from "lucide-react";
import { Body, BottomBar, ButtonLink, ErrorText, List, PrimaryButton, Row, Screen, Sheet, TextButton, TextField, TopBar } from "@/components/kit";
import { api, errorMessage, type Member } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function MembersPage() {
  const { group, reload } = useGroup();
  const pid = group.public_id;
  const [adding, setAdding] = useState(false);
  const [editing, setEditing] = useState<Member | null>(null);
  const [name, setName] = useState("");
  const [error, setError] = useState<string | null>(null);

  const canRename = (m: Member) => !group.closed && (m.is_me || (group.is_owner && !m.claimed));

  async function save() {
    setError(null);
    try {
      if (editing) {
        await api(`/groups/${pid}/members/${editing.id}`, { method: "PATCH", body: { display_name: name.trim() } });
      } else {
        await api(`/groups/${pid}/members`, { method: "POST", body: { names: [name.trim()] } });
      }
      setAdding(false);
      setEditing(null);
      setName("");
      await reload();
    } catch (e) {
      setError(errorMessage(e));
    }
  }

  return (
    <Screen>
      <TopBar title="メンバー" subtitle={group.title} back={`/g/${pid}`} />
      <Body>
        <List aria-label="メンバー一覧" className="mt-4">
          {group.members.map((m) => (
            <Row
              key={m.id}
              label={`${m.display_name}${m.is_me ? "(あなた)" : ""}`}
              value={m.role === "owner" ? "作成者" : m.claimed ? "" : "名前のみ"}
              onClick={canRename(m) ? () => { setEditing(m); setName(m.display_name); } : undefined}
              chevron={canRename(m)}
            />
          ))}
        </List>
        {!group.closed && (
          <TextButton className="mt-2" onClick={() => { setAdding(true); setName(""); }}>
            <Plus className="size-5" aria-hidden />
            メンバーを追加
          </TextButton>
        )}
        {error && <ErrorText>{error}</ErrorText>}
      </Body>
      {group.is_owner && !group.closed && (
        <BottomBar>
          <ButtonLink href={`/g/${pid}/invite`} variant="outline">
            <Link2 className="size-5" aria-hidden />
            招待リンク
          </ButtonLink>
        </BottomBar>
      )}
      <Sheet
        open={adding || editing !== null}
        onOpenChange={(v) => { if (!v) { setAdding(false); setEditing(null); } }}
        title={editing ? "名前を変更" : "メンバーを追加"}
        footer={<PrimaryButton disabled={!name.trim()} onClick={save}>{editing ? "変更する" : "追加する"}</PrimaryButton>}
      >
        <TextField label="名前" value={name} onChange={setName} maxLength={40} autoFocus className="mt-2" />
      </Sheet>
    </Screen>
  );
}
