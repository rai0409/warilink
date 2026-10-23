"use client";

import { SettlementPanel } from "@/components/SettlementPanel";
import { useGroup } from "@/lib/group-context";

export default function SettlePage() {
  const { group } = useGroup();
  return <SettlementPanel group={group} />;
}
