import { Popover, PopoverButton, PopoverPanel } from "@headlessui/react";
import { TextAlignJustify } from "lucide-react";
import Link from "next/link";

export default function Header() {
  return (
    <div className="flex flex-row w-full justify-between items-center p-8">
      <h1 className="text-3xl">Camino</h1>
      <Popover className="relative">
        <PopoverButton>
          <TextAlignJustify />
        </PopoverButton>
        <PopoverPanel anchor="bottom" className="flex flex-col gap-4 p-4">
          <div>
            <Link href="/">Home</Link>
          </div>
          <div>
            <Link href="/tours">Tours</Link>
          </div>
          <div>
            <Link href="/generate">Generate</Link>
          </div>
          <div>
            <Link href="/settings">Settings</Link>
          </div>
        </PopoverPanel>
      </Popover>
    </div>
  );
}
