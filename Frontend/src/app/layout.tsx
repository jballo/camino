import type { Metadata } from "next";
import { Bungee, Bungee_Shade, JetBrains_Mono, Space_Grotesk } from "next/font/google";
import "./globals.css";
import { ClerkProvider } from "@clerk/nextjs";
import Header from "@/components/header";

const fontSans = Space_Grotesk({
  subsets: ["latin"],
  variable: "--font-sans",
});

const fontDisplay = Bungee({
  subsets: ["latin"],
  variable: "--font-display",
  weight: "400",
});

const fontShade = Bungee_Shade({
  subsets: ["latin"],
  variable: "--font-shade",
  weight: "400",
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
      className={`${fontSans.variable} ${fontDisplay.variable} ${fontShade.variable} ${fontMono.variable} h-full antialiased dark`}
    >
      <body className="h-full overflow-hidden flex flex-col bg-background text-foreground">
        <ClerkProvider appearance={{ variables: { colorBackground: "#151517", colorText: "#e8e7e3", colorPrimary: "#df9058", colorInputBackground: "#1f1f22", colorInputText: "#e8e7e3", borderRadius: "0.625rem" } }}>
          <Header />
          <main className="flex-1 min-h-0 overflow-y-auto">{children}</main>
        </ClerkProvider>
      </body>
    </html>
  );
}
