"use client";

import { Show, SignInButton, UserButton } from "@clerk/nextjs";
import { Popover, PopoverButton, PopoverPanel } from "@headlessui/react";
import { Menu } from "lucide-react";
import Link from "next/link";
import { usePathname } from "next/navigation";

type NavLink = { href: string; label: string };

const APP_LINKS: NavLink[] = [
  { href: "/briefs", label: "Briefs" },
  { href: "/explore", label: "Explore" },
  { href: "/tours", label: "Tours" },
  { href: "/settings", label: "Settings" },
];

const VISITOR_LINKS: NavLink[] = [{ href: "/#trail", label: "How it works" }];

function isActive(pathname: string, href: string) {
  if (href === "/") return pathname === "/";
  return pathname === href || pathname.startsWith(`${href}/`);
}

function DesktopNav({ links, pathname }: { links: NavLink[]; pathname: string }) {
  return (
    <nav className="hidden items-center gap-[20px] md:flex">
      {links.map((link) => {
        const className =
          "px-[2px] py-2 font-mono text-[9.5px] uppercase tracking-[.18em] text-muted-foreground transition hover:text-foreground";
        // Hash links use a plain anchor: the page scrolls inside <main>, which
        // the browser's own fragment scrolling handles and Link does not.
        return link.href.includes("#") ? (
          <a key={link.href} href={link.href} className={className}>
            {link.label}
          </a>
        ) : (
          <Link
            key={link.href}
            href={link.href}
            aria-current={isActive(pathname, link.href) ? "page" : undefined}
            className={className}
          >
            {link.label}
          </Link>
        );
      })}
    </nav>
  );
}

function MobileNav({ links, pathname }: { links: NavLink[]; pathname: string }) {
  return (
    <Popover className="relative md:hidden">
      <PopoverButton
        className="flex size-11 items-center justify-center border-[1.5px] border-input hover:bg-accent"
        aria-label="Open menu"
      >
        <Menu className="size-5" />
      </PopoverButton>
      <PopoverPanel
        anchor="bottom end"
        className="mt-2 flex w-56 flex-col rounded-[10px] border border-border bg-popover p-2 shadow-2xl"
      >
        {links.map((link) => (
          <PopoverButton
            key={link.href}
            as={link.href.includes("#") ? "a" : Link}
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
  );
}

export default function Header() {
  const pathname = usePathname();
  // From the landing page, signing in or up should land in the workbench.
  const landingRedirect = pathname === "/" ? "/briefs" : undefined;

  return (
    <header className="z-40 flex h-[69.5px] w-full shrink-0 flex-row items-center justify-between gap-4 border-b border-border px-5 sm:px-[30px]">
      <div className="flex items-center gap-[31.25px]">
        <Link href="/" className="font-display text-[20.5px] uppercase leading-none">
          Camino
        </Link>
        <Show when="signed-in">
          <DesktopNav links={APP_LINKS} pathname={pathname} />
        </Show>
        <Show when="signed-out">
          <DesktopNav links={VISITOR_LINKS} pathname={pathname} />
        </Show>
      </div>

      <div className="flex flex-row items-center gap-2">
        <Show when="signed-in">
          <UserButton />
        </Show>
        <Show when="signed-out">
          <SignInButton
            mode="modal"
            forceRedirectUrl={landingRedirect}
            signUpForceRedirectUrl={landingRedirect}
          >
            <button className="inline-flex h-[35.5px] w-[88px] items-center justify-center border-[1.5px] border-foreground font-mono text-[9.5px] uppercase tracking-[.18em] transition hover:bg-foreground hover:text-background">
              Sign in
            </button>
          </SignInButton>
        </Show>

        <Show when="signed-in">
          <MobileNav links={APP_LINKS} pathname={pathname} />
        </Show>
        <Show when="signed-out">
          <MobileNav links={VISITOR_LINKS} pathname={pathname} />
        </Show>
      </div>
    </header>
  );
}
