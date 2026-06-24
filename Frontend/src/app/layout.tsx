import type { Metadata } from "next";
import { Outfit, Merriweather, JetBrains_Mono } from "next/font/google";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";

const fontSans = Outfit({
  subsets: ["latin"],
  variable: "--font-sans",
});

const fontSerif = Merriweather({
  subsets: ["latin"],
  variable: "--font-serif",
  weight: ["400", "700"],
});

const fontMono = JetBrains_Mono({
  subsets: ["latin"],
  variable: "--font-mono",
});

export const metadata: Metadata = {
  title: "Camino",
  description:
    "Guided tours of unfamiliar codebases — connect a repo, pick a topic, get a structured walkthrough.",
};

export default function RootLayout({
  children,
}: Readonly<{
  children: React.ReactNode;
}>) {
  return (
    <html
      lang="en"
      className={`${fontSans.variable} ${fontSerif.variable} ${fontMono.variable} h-full antialiased dark`}
    >
      <body className="min-h-full flex flex-col bg-background text-foreground">
        <ClerkProvider>{children}</ClerkProvider>
      </body>
    </html>
  );
}
