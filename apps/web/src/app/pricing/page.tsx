"use client";

import { useEffect, useState } from "react";
import { Body, ErrorText, Screen, SectionTitle, TopBar } from "@/components/kit";
import { api, errorMessage, type Plans } from "@/lib/api";

type Plan = Plans["plans"][number];

/** 読み取り枚数。無料プランの上限は広告の視聴で増える分なので、広告を止めているときは含めない */
function scansLabel(p: Plan, adsAvailable: boolean) {
  const max = adsAvailable || p.id !== "free" ? p.ocr_monthly_scans_max : p.ocr_monthly_scans_min;
  return p.ocr_monthly_scans_min === max ? `${max}枚` : `${p.ocr_monthly_scans_min}〜${max}枚`;
}

/** プランの説明(サーバーの文言)から、止めている機能の説明を外し、停止中であることを書き足す */
function visibleNotes(notes: string[], plans: Plans) {
  // 広告で増えるのは読み取り枚数なので、広告の説明は読み取りと広告の両方が使えるときだけ出す
  const out = notes.filter((n) => (plans.ocr_available || !n.includes("読み取り"))
    && ((plans.ads_available && plans.ocr_available) || !n.includes("広告")));
  if (!plans.ocr_available && out.length < notes.length) out.push("レシート読み取りは停止中です(手入力で登録できます)");
  else if (!plans.ads_available && out.length < notes.length) out.push("広告の視聴による読み取り枚数の追加は準備中です");
  return out;
}

export default function PricingPage() {
  const [plans, setPlans] = useState<Plans | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Plans>("/plans").then(setPlans).catch((e) => setError(errorMessage(e)));
  }, []);

  return (
    <Screen>
      <TopBar title="料金プラン" back="/settings" />
      <Body>
        {error && <ErrorText>{error}</ErrorText>}
        {/* OCR・広告・有料プランを止めていても料金表は表示する。止めている機能を、使えるもののように見せない */}
        {plans && (
          <>
            <p className="mt-4 text-[13px] text-warning">
              {plans.ocr_available
                ? "レシート読み取りは無料枠の範囲で利用できます。広告・有料プランは準備中で、購入や広告の視聴はできません。"
                : "いま提供しているのは、登録不要・手入力の割り勘と精算(無料)です。レシート読み取りは停止中です。広告・有料プランは準備中で、購入や広告の視聴はできません。"}
            </p>
            <table aria-label="料金プランの比較" className="mt-4 w-full border-t border-border text-[13px]">
              <thead>
                <tr className="border-b border-border text-muted-foreground">
                  <th scope="col" className="py-2 text-left font-normal">プラン</th>
                  <th scope="col" className="py-2 text-right font-normal">料金</th>
                  <th scope="col" className="py-2 text-right font-normal">レシート読み取り/月</th>
                  <th scope="col" className="py-2 text-right font-normal">状態</th>
                </tr>
              </thead>
              <tbody>
                {plans.plans.map((p) => (
                  <tr key={p.id} className="border-b border-border">
                    <th scope="row" className="py-3 text-left font-medium">{p.name}</th>
                    <td className="py-3 text-right">{p.price_label}</td>
                    <td className="py-3 text-right tabular-nums">
                      {!plans.ocr_available ? "停止中" : scansLabel(p, plans.ads_available)}
                    </td>
                    <td className="py-3 text-right">{!p.available ? "検討中" : plans.ocr_available ? "利用できます" : "割り勘は利用可・読み取りは停止中"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {plans.plans.map((p) => (
              <section key={p.id} aria-label={p.name}>
                <SectionTitle>{p.name}</SectionTitle>
                <ul className="list-disc space-y-1 pl-5 text-[13px]">
                  {visibleNotes(p.notes, plans).map((n) => (
                    <li key={n}>{n}</li>
                  ))}
                </ul>
              </section>
            ))}
          </>
        )}
      </Body>
    </Screen>
  );
}
