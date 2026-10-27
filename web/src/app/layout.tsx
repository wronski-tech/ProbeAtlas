import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ProbeAtlas",
  description: "Internet infrastructure monitoring from distributed agents",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
