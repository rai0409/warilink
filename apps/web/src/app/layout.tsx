import type { Metadata, Viewport } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "WariLink(開発版)",
  description: "登録不要の割り勘・精算アプリ",
};

export const viewport: Viewport = { width: "device-width", initialScale: 1 };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="ja">
      <body>
        <main>
          <div className="banner" role="note">
            開発版です。このアプリは送金を実行しません。金額と送金先の案内のみを行います。
          </div>
          {children}
          <footer className="footer">
            <Link href="/">トップ</Link> · <Link href="/pricing">料金プラン</Link>
          </footer>
        </main>
      </body>
    </html>
  );
}
