import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "ContractBridge — Self-healing API integrations",
  description: "Recover from API contract drift with a verified runtime bridge, then track the permanent client repair.",
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
