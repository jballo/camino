"use client";

import { Show, SignInButton, UserButton } from "@clerk/nextjs";
import { Popover, PopoverButton, PopoverPanel } from "@headlessui/react";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

const NAV_LINKS = [
  { href: "/", label: "Home" },
  { href: "/explore", label: "Explore" },
  { href: "/tours", label: "Tours" },
  { href: "/settings", label: "Settings" },
] as const;

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

export default function Header() {
  const pathname = usePathname();

  return (
    <header className="z-40 flex h-20 w-full shrink-0 flex-row items-center justify-between gap-4 border-b border-border/70 bg-background/85 px-5 backdrop-blur-xl sm:px-8">
      <div className="flex items-center gap-8">
        <Link href="/" className="font-display text-2xl font-black uppercase tracking-[.08em]">
          Camino
        </Link>
        <nav className="hidden items-center gap-1 md:flex">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className={`border-b-2 px-3 py-2 font-mono text-[11px] uppercase tracking-[.13em] transition ${
                isActive(pathname, link.href)
                  ? "border-brand-accent text-foreground"
                  : "border-transparent text-muted-foreground hover:text-foreground"
              }`}
            >
              {link.label}
            </Link>
          ))}
        </nav>
      </div>

      <div className="flex flex-row items-center gap-2">
        <Show when="signed-in">
          <UserButton />
        </Show>
        <Show when="signed-out">
          <SignInButton mode="modal">
            <button className="button-ghost min-h-10 px-4">
              Sign in
            </button>
          </SignInButton>
        </Show>

        <Popover className="relative md:hidden">
          <PopoverButton
            className="flex size-11 items-center justify-center rounded-full border border-border hover:bg-accent"
            aria-label="Open menu"
          >
            <Menu className="size-5" />
          </PopoverButton>
          <PopoverPanel
            anchor="bottom end"
            className="mt-2 flex w-56 flex-col rounded-[14px] border border-border bg-popover p-2 shadow-2xl"
          >
            {NAV_LINKS.map((link) => (
              <PopoverButton
                key={link.href}
                as={Link}
                href={link.href}
                className={`rounded-lg px-3 py-3 font-mono text-xs uppercase tracking-wider transition hover:bg-accent ${
                  isActive(pathname, link.href)
                    ? "text-foreground"
                    : "text-muted-foreground"
                }`}
              >
                {link.label}
              </PopoverButton>
            ))}
          </PopoverPanel>
        </Popover>
      </div>
    </header>
  );
}
