import type { Metadata } from "next";
import { AccountPanel } from "@/components/AccountPanel";
import { Body, Screen, TopBar } from "@/components/kit";
import { DestinationPanel } from "./DestinationPanel";

// 本人のログイン状態を扱うページ。検索エンジンに載せない
export const metadata: Metadata = { title: "アカウントと Passkey", robots: { index: false, follow: false } };

export default function AccountPage() {
  return (
    <Screen>
      <TopBar title="アカウントと Passkey" back="/settings" />
      <Body>
        <AccountPanel />
        <DestinationPanel />
      </Body>
    </Screen>
  );
}
