import type { Metadata } from "next";
import { Doto, JetBrains_Mono, Space_Grotesk } from "next/font/google";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import Header from "@/components/header";

const fontSans = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-sans",
});

const fontDisplay = Doto({
  subsets: ["latin"],
  variable: "--font-display",
  weight: ["700", "900"],
});

const fontMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Camino",
  description:
    "Open-source contribution helper — paste a GitHub issue and get a grounded implementation brief, plus guided tours of the code it touches.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${fontSans.variable} ${fontDisplay.variable} ${fontMono.variable} h-full antialiased dark`}
    >
      <body className="h-full overflow-hidden flex flex-col bg-background text-foreground">
        <ClerkProvider appearance={{ variables: { colorBackground: "oklch(0.1822 0 0)", colorText: "oklch(0.8109 0 0)", colorPrimary: "oklch(0.7214 0.1337 49.9802)", colorInputBackground: "oklch(0.252 0 0)", colorInputText: "oklch(0.8109 0 0)", borderRadius: "0.875rem" } }}>
          <div className="h-0.5 shrink-0 bg-brand-accent" aria-hidden="true" />
          <Header />
          <main className="flex-1 min-h-0 overflow-y-auto">{children}</main>
        </ClerkProvider>
      </body>
    </html>
  );
}
