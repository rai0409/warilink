import type { NextConfig } from "next";

const apiOrigin = process.env.API_ORIGIN ?? "http://localhost:8000";

const nextConfig: NextConfig = {
  output: "standalone",
  outputFileTracingRoot: new URL("../../", import.meta.url).pathname,
  // 開発時の左下のボタンが、スマホ幅で画面下の主ボタン・シートのボタンに重なるため出さない
  devIndicators: false,
  // スマホなど別の端末から開発サーバーを開くとき(DEV_ORIGINS=192.168.x.x)。未設定なら localhost だけ
  allowedDevOrigins: process.env.DEV_ORIGINS?.split(",").map((s) => s.trim()).filter(Boolean),
  // API を同一オリジンに見せ、Cookie を SameSite=Lax のまま使う
  async rewrites() {
    return [{ source: "/api/v1/:path*", destination: `${apiOrigin}/api/v1/:path*` }];
  },
  async headers() {
    // グループ・招待・精算ページは非公開。検索エンジンに載せない
    const noindex = [{ key: "X-Robots-Tag", value: "noindex, nofollow" }];
    return [
      { source: "/g/:path*", headers: noindex },
      { source: "/join/:path*", headers: noindex },
    ];
  },
};

export default nextConfig;
