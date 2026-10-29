import { FileText, Image as ImageIcon } from "lucide-react";
import { Body, List, Muted, Row, Screen, SectionTitle, TopBar } from "@/components/kit";

// 期間はサーバーの既定値(receipt_image_retention_days / audit_log_retention_days)と同じ
export default function RetentionPage() {
  return (
    <Screen>
      <TopBar title="データの保存期間" back="/settings" />
      <Body>
        <List aria-label="データの保存期間" className="mt-4">
          <Row icon={<ImageIcon />} label="画像" sub="レシートの写真" value={<span className="text-foreground">30日</span>} chevron={false} />
          <Row icon={<FileText />} label="操作の記録" sub="不正利用の調査のための記録" value={<span className="text-foreground">1年</span>} chevron={false} />
        </List>
        <Muted className="mt-3">期間を過ぎると自動で削除されます。</Muted>
        <div className="mt-10 border-t border-border pt-2">
          <SectionTitle className="text-[14px] text-foreground">退会後に残る情報</SectionTitle>
          <Muted>グループには名前と過去の記録が残ります。</Muted>
        </div>
      </Body>
    </Screen>
  );
}
