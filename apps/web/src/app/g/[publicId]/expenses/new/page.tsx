"use client";

import { useRouter } from "next/navigation";
import { ExpenseForm } from "@/components/ExpenseForm";
import { useGroup } from "@/lib/group-context";

export default function NewExpensePage() {
  const { group } = useGroup();
  const router = useRouter();
  return (
    <ExpenseForm
      publicId={group.public_id}
      members={group.members}
      myMemberId={group.my_member_id}
      onSaved={() => router.push(`/g/${group.public_id}`)}
    />
  );
}
