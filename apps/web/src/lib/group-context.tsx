"use client";

import { createContext, useContext } from "react";
import type { Group } from "@/lib/api";

type Ctx = { group: Group; reload: () => Promise<void> };

export const GroupContext = createContext<Ctx | null>(null);

/** /g/[publicId] 配下の画面で、レイアウトが読み込んだグループ情報を使う。 */
export function useGroup(): Ctx {
  const ctx = useContext(GroupContext);
  if (!ctx) throw new Error("useGroup は GroupContext の内側で使う");
  return ctx;
}

export function memberNames(group: Group): Record<string, string> {
  return Object.fromEntries(group.members.map((m) => [m.id, m.display_name]));
}
