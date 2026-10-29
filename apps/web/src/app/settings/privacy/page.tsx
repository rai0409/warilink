import { Body, Muted, Screen, TopBar } from "@/components/kit";

export default function Page() {
  return (
    <Screen>
      <TopBar title="プライバシーポリシー" back="/settings" />
      <Body>
        <Muted className="mt-6">。公開までお待ちください。</Muted>
      </Body>
    </Screen>
  );
}
