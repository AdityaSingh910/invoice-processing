import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import Script from "next/script";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { ThemeProvider } from "@/lib/theme";
import { ToastProvider } from "@/components/ui/Toast";

/**
 * Runs before hydration so a returning dark-mode user never sees a flash of
 * the light theme. Reads localStorage directly rather than waiting on React,
 * which is why ThemeProvider (lib/theme.tsx) is a separate, tiny sync step.
 */
const THEME_BOOTSTRAP = `
(function () {
  try {
    if (localStorage.getItem("ip-theme") === "dark") {
      document.documentElement.setAttribute("data-theme", "dark");
    }
  } catch (e) {}
})();
`;

/**
 * Both self-hosted by next/font at build time — no runtime request to Google,
 * which matters because the production bundle is served by FastAPI on a machine
 * that may have no outbound network.
 *
 * Plex Mono carries every figure that must be scanned and compared — money,
 * invoice/PO numbers, dates, run IDs (`.tnum` in globals.css) — the same
 * instinct Mercury and Ramp lean on: numbers set in a fixed-width face read as
 * ledger entries, not as UI chrome.
 */
const plexSans = IBM_Plex_Sans({
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
  display: "swap",
  variable: "--font-sans",
});

const plexMono = IBM_Plex_Mono({
  subsets: ["latin"],
  weight: ["400", "500", "600"],
  display: "swap",
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Invoice Processing",
  description: "Accounts payable automation — the AI reads, the rules decide.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable}`} suppressHydrationWarning>
      <head>
        <Script id="theme-bootstrap" strategy="beforeInteractive">
          {THEME_BOOTSTRAP}
        </Script>
      </head>
      <body>
        <ThemeProvider>
          <AuthProvider>
            <ToastProvider>{children}</ToastProvider>
          </AuthProvider>
        </ThemeProvider>
      </body>
    </html>
  );
}
