"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ExpenseForm } from "@/components/ExpenseForm";
import { ReceiptUpload } from "@/components/ReceiptUpload";
import { api, type Plans } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function NewExpensePage() {
  const { group } = useGroup();
  const router = useRouter();
  const [ocr, setOcr] = useState(false);

  // 読み取りを使えるときは、金額を入れる前にレシートを撮れるようにする(読み取り後は商品別の確認画面へ)
  useEffect(() => {
    api<Plans>("/plans").then((p) => setOcr(p.ocr_available)).catch(() => setOcr(false));
  }, []);

  return (
    <ExpenseForm
      publicId={group.public_id}
      members={group.members}
      myMemberId={group.my_member_id}
      backHref={`/g/${group.public_id}`}
      onSaved={() => router.push(`/g/${group.public_id}`)}
      header={ocr && !group.closed ? <ReceiptUpload publicId={group.public_id} available /> : undefined}
    />
  );
}
