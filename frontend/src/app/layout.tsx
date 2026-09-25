import type { Metadata } from "next";
import { Big_Shoulders_Stencil, Chakra_Petch, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { AuthProvider } from "@/lib/auth";
import Nav from "@/components/Nav";

// Three faces, three jobs: stencil for headings and big numerals, Chakra Petch
// for reading, JetBrains Mono for every figure that has to line up.
const stencil = Big_Shoulders_Stencil({ variable: "--font-stencil", subsets: ["latin"] });
const body = Chakra_Petch({
  variable: "--font-body",
  subsets: ["latin"],
  weight: ["400", "500", "600", "700"],
});
const mono = JetBrains_Mono({ variable: "--font-mono", subsets: ["latin"] });

export const metadata: Metadata = {
  title: "Draft War Room",
  description: "Fantasy basketball draft prep",
};

// The layout itself stays a server component; only AuthProvider and Nav are
// client ones, so the static export still prerenders the shell.
export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="en" className={`${stencil.variable} ${body.variable} ${mono.variable}`}>
      <body>
        <AuthProvider>
          <Nav />
          <main className="wrap">{children}</main>
        </AuthProvider>
      </body>
    </html>
  );
}
