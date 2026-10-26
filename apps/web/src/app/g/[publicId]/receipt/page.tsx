"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { ReceiptEditor } from "@/components/ReceiptEditor";
import { ReceiptUpload } from "@/components/ReceiptUpload";
import { api, type Plans } from "@/lib/api";
import { useGroup } from "@/lib/group-context";

export default function ReceiptPage() {
  const { group } = useGroup();
  const router = useRouter();
  const [plans, setPlans] = useState<Plans | null>(null);

  useEffect(() => {
    api<Plans>("/plans").then(setPlans).catch(() => setPlans(null));
  }, []);

  return (
    <>
      {plans?.ocr_available && <section className="card" aria-labelledby="ocr-h">
        <h2 id="ocr-h">レシート画像から読み取る</h2>
        {plans && <ReceiptUpload publicId={group.public_id} available={plans.ocr_available} />}
        <p className="muted">
          画像は内容の確認のために保存し、一定期間後に削除します。撮影位置などの情報(EXIF)は保存しません。{" "}
          <Link href="/pricing">料金プラン</Link>
        </p>
      </section>}
      <ReceiptEditor
        publicId={group.public_id}
        members={group.members}
        myMemberId={group.my_member_id}
        onSaved={(id) => router.push(`/g/${group.public_id}/expenses/${id}`)}
      />
    </>
  );
}
