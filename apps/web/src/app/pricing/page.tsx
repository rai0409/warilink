"use client";

import { useEffect, useState } from "react";
import { Body, ErrorText, Screen, SectionTitle, TopBar } from "@/components/kit";
import { api, errorMessage, type Plans } from "@/lib/api";

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
        {/* OCR を止めていても料金表は表示する(読み取りは「停止中」と明示し、利用できるとは示さない) */}
        {plans && (
          <>
            <p className="mt-4 text-[13px] text-warning">
              {plans.ocr_available
                ? "レシート読み取りは無料枠の範囲で利用できます。広告・有料プランは準備中で、購入や広告の視聴はできません。"
                : "現在、レシート読み取りは停止中です(手入力は利用できます)。広告・有料プランは準備中で、購入や広告の視聴はできません。"}
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
                      {p.ocr_monthly_scans_min === p.ocr_monthly_scans_max
                        ? `${p.ocr_monthly_scans_min}枚`
                        : `${p.ocr_monthly_scans_min}〜${p.ocr_monthly_scans_max}枚`}
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
                  {p.notes.map((n) => (
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
