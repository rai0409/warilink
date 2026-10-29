import { Body, Muted, Screen, TopBar } from "@/components/kit";

export default function Page() {
  return (
    <Screen>
      <TopBar title="利用規約" back="/settings" />
      <Body>
        <Muted className="mt-6">。公開までお待ちください。</Muted>
      </Body>
    </Screen>
  );
}
