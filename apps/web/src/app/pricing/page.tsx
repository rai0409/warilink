"use client";

import { useEffect, useState } from "react";
import { api, errorMessage, type Plans } from "@/lib/api";

export default function PricingPage() {
  const [plans, setPlans] = useState<Plans | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    api<Plans>("/plans").then(setPlans).catch((e) => setError(errorMessage(e)));
  }, []);

  return (
    <>
      <h1>料金プラン</h1>
      {error && <p className="error" role="alert">{error}</p>}
      {plans && (
        <>
          <p className="notice warn">
            {plans.ocr_available
              ? "レシート読み取りは無料枠の範囲で利用できます。広告・有料プランは準備中で、購入や広告の視聴はできません。"
              : "現在、レシート読み取りは停止中です(手入力は利用できます)。広告・有料プランは準備中で、購入や広告の視聴はできません。"}
          </p>
          <table aria-label="料金プランの比較">
            <thead>
              <tr>
                <th scope="col">プラン</th>
                <th scope="col">料金</th>
                <th scope="col">レシート読み取り/月</th>
                <th scope="col">状態</th>
              </tr>
            </thead>
            <tbody>
              {plans.plans.map((p) => (
                <tr key={p.id}>
                  <th scope="row">{p.name}</th>
                  <td>{p.price_label}</td>
                  <td>
                    {p.ocr_monthly_scans_min === p.ocr_monthly_scans_max
                      ? `${p.ocr_monthly_scans_min}枚`
                      : `${p.ocr_monthly_scans_min}〜${p.ocr_monthly_scans_max}枚`}
                  </td>
                  <td>{!p.available ? "検討中" : plans.ocr_available ? "利用できます" : "割り勘は利用可・読み取りは停止中"}</td>
                </tr>
              ))}
            </tbody>
          </table>
          {plans.plans.map((p) => (
            <section key={p.id} className="card" aria-label={p.name}>
              <h2>{p.name}</h2>
              <ul>
                {p.notes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            </section>
          ))}
        </>
      )}
    </>
  );
}
