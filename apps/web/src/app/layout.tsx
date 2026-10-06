import "./globals.css";

import "@fontsource-variable/inter/opsz.css";
import "@fontsource-variable/plus-jakarta-sans";
import "@fontsource-variable/jetbrains-mono";
import type { Metadata, Viewport } from "next";

import { THEME_BOOTSTRAP } from "@/lib/theme-script";

export const metadata: Metadata = {
  title: { default: "Research", template: "%s · Research" },
  description: "AI-native B2B lead intelligence — fresh, evidence-backed leads on demand.",
  icons: { icon: "/icon.svg" },
};

export const viewport: Viewport = {
  themeColor: [
    { media: "(prefers-color-scheme: dark)", color: "#000000" },
    { media: "(prefers-color-scheme: light)", color: "#ffffff" },
  ],
  colorScheme: "dark light",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" data-theme="dark" suppressHydrationWarning>
      <head>
        <script dangerouslySetInnerHTML={{ __html: THEME_BOOTSTRAP }} />
      </head>
      <body>{children}</body>
    </html>
  );
}
