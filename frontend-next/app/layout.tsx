import type { Metadata } from "next";
import { IBM_Plex_Mono, IBM_Plex_Sans } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import { ToastProvider } from "@/components/ui/Toast";

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
    <html lang="en" className={`${plexSans.variable} ${plexMono.variable}`}>
      <body>
        <AuthProvider>
          <ToastProvider>{children}</ToastProvider>
        </AuthProvider>
      </body>
    </html>
  );
}
