import Link from "next/link";
import { Body, Muted, Screen, TopBar } from "@/components/kit";

/**
 * 利用規約・プライバシーポリシーの公開前の表示。文面は確定していない(docs/legal/ の下書きは運営者と専門家の確認待ち)ため、
 * 下書きを正式な条件として見せない。
 */
export function DraftDocument({ title }: { title: string }) {
  return (
    <Screen>
      <TopBar title={title} back="/settings" />
      <Body>
        <p role="status" className="mt-6 text-[14px]">{title}は準備中です。</p>
        <Muted className="mt-2">
          内容を確定してから、この画面で公開します。確定前の下書きを正式な条件として扱うことはありません。
        </Muted>
        <Muted className="mt-6">
          データの保存期間は<Link href="/settings/retention">こちら</Link>で確認できます。
        </Muted>
      </Body>
    </Screen>
  );
}
