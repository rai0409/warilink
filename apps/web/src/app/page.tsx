"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { Ellipsis, UsersRound } from "lucide-react";
import { Body, ButtonLink, List, Muted, Row, Screen, SectionTitle } from "@/components/kit";
import { api, type Me } from "@/lib/api";

type GroupSummary = { public_id: string; title: string; member_count: number; is_owner: boolean; closed: boolean };

export default function HomePage() {
  const [groups, setGroups] = useState<GroupSummary[] | null>(null);

  useEffect(() => {
    // セッションがまだ無い(初めての)ときは何も作らない。グループを作る・参加するときにゲストを作る
    api<Me | null>("/identity/me")
      .then((me) => (me ? api<GroupSummary[]>("/groups") : []))
      .then(setGroups)
      .catch(() => setGroups([]));
  }, []);

  return (
    <Screen>
      <header className="flex items-center justify-between px-6 pt-6">
        <span className="text-[22px] font-semibold tracking-tight">WariLink</span>
        <Link href="/settings" aria-label="設定" className="grid size-11 place-items-center text-foreground">
          <Ellipsis className="size-5" aria-hidden />
        </Link>
      </header>
      <Body className="flex flex-col">
        <div className="mt-24 space-y-2">
          <ButtonLink href="/new">グループを作る</ButtonLink>
          <Link href="/join" className="flex min-h-11 items-center justify-center text-[14px] font-medium no-underline">
            招待リンクから参加
          </Link>
        </div>
        {groups && groups.length > 0 && (
          <>
            <SectionTitle className="mt-10">最近のグループ</SectionTitle>
            <List aria-label="最近のグループ">
              {groups.map((g) => (
                <Row
                  key={g.public_id}
                  href={`/g/${g.public_id}`}
                  icon={<UsersRound strokeWidth={1.6} />}
                  label={g.title}
                  sub={`${g.member_count}人${g.closed ? " · 終了" : ""}`}
                />
              ))}
            </List>
          </>
        )}
        <Muted className="mt-auto pt-16">登録不要で使えます</Muted>
      </Body>
    </Screen>
  );
}
