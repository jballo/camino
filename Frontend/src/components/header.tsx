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
    <header className="flex w-full flex-row items-center justify-between gap-4 p-8">
      <div className="flex items-center gap-8">
        <Link href="/" className="text-3xl">
          Camino
        </Link>
        <nav className="hidden items-center gap-1 md:flex">
          {NAV_LINKS.map((link) => (
            <Link
              key={link.href}
              href={link.href}
              className={`rounded-md px-3 py-1.5 text-sm transition hover:bg-accent ${
                isActive(pathname, link.href)
                  ? "text-foreground"
                  : "text-muted-foreground"
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
            <button className="rounded-md bg-primary px-4 py-1.5 text-sm text-primary-foreground transition hover:opacity-90">
              Sign in
            </button>
          </SignInButton>
        </Show>

        <Popover className="relative md:hidden">
          <PopoverButton
            className="flex size-9 items-center justify-center rounded-md hover:bg-accent"
            aria-label="Open menu"
          >
            <Menu className="size-5" />
          </PopoverButton>
          <PopoverPanel
            anchor="bottom end"
            className="mt-2 flex w-44 flex-col rounded-lg border border-border bg-popover p-1 shadow-md"
          >
            {NAV_LINKS.map((link) => (
              <PopoverButton
                key={link.href}
                as={Link}
                href={link.href}
                className={`rounded-md px-3 py-2 text-sm transition hover:bg-accent ${
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
