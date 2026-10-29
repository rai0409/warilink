import type { Metadata } from "next";
import { AccountPanel } from "@/components/AccountPanel";
import { DeleteAccountPanel } from "./DeleteAccountPanel";
import { DestinationPanel } from "./DestinationPanel";

// 本人のログイン状態を扱うページ。検索エンジンに載せない
export const metadata: Metadata = { title: "アカウントと Passkey", robots: { index: false, follow: false } };

export default function AccountPage() {
  return (
    <>
      <h1>アカウントと Passkey</h1>
      <AccountPanel />
      <DestinationPanel />
      <DeleteAccountPanel />
    </>
  );
}
