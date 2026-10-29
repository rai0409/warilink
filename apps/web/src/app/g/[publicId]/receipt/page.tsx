"use client";

import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { ReceiptUpload } from "@/components/ReceiptUpload";
import { api, type Plans } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

/** 商品別の支出を手で入力する(OCR を提供中なら、画像から読み取る入口も出す) */
export default function ReceiptPage() {
  const { group } = useGroup();
  const router = useRouter();
  const [plans, setPlans] = useState<Plans | null>(null);

  useEffect(() => {
    api<Plans>("/plans").then(setPlans).catch(() => setPlans(null));
  }, []);

  return (
    <ReceiptEditor
      publicId={group.public_id}
      members={group.members}
      myMemberId={group.my_member_id}
      backHref={`/g/${group.public_id}/expenses/new`}
      header={plans?.ocr_available ? <ReceiptUpload publicId={group.public_id} available /> : undefined}
      onSaved={(id) => router.push(`/g/${group.public_id}/expenses/${id}`)}
    />
  );
}
