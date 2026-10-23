"use client";

import Link from "next/link";
import { useParams, usePathname } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { api, errorMessage, type Group } from "@/lib/api";
import { GroupContext } from "@/lib/group-context";

const TABS = [
  { href: "", label: "概要" },
  { href: "/expenses/new", label: "支出を追加" },
  { href: "/receipt", label: "レシート" },
  { href: "/expenses", label: "探す" },
  { href: "/settle", label: "精算" },
  { href: "/stats", label: "集計" },
  { href: "/settings", label: "設定" },
];

export default function GroupLayout({ children }: { children: React.ReactNode }) {
  const { publicId } = useParams<{ publicId: string }>();
  const pathname = usePathname();
  const [group, setGroup] = useState<Group | null>(null);
  const [error, setError] = useState<string | null>(null);

  const reload = useCallback(async () => {
    try {
      setGroup(await api<Group>(`/groups/${publicId}`));
      setError(null);
    } catch (e) {
      setError(errorMessage(e));
    }
  }, [publicId]);

  useEffect(() => {
    reload();
  }, [reload]);

  if (!group) return error ? <p className="error" role="alert">{error}</p> : <p className="muted">読み込み中…</p>;
  const base = `/g/${publicId}`;
  return (
    <GroupContext.Provider value={{ group, reload }}>
      <h1>{group.title}</h1>
      <nav className="tabs" aria-label="グループのメニュー">
        {TABS.map((t) => {
          const href = base + t.href;
          // 「探す」(/expenses)は「支出を追加」(/expenses/new)と重ならないよう完全一致で判定する
          const current =
            t.href === "" || t.href === "/expenses" ? pathname === href : pathname.startsWith(href);
          return (
            <Link key={t.href} href={href} aria-current={current ? "page" : undefined}>
              {t.label}
            </Link>
          );
        })}
      </nav>
      {children}
    </GroupContext.Provider>
  );
}
