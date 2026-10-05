"use client";

import { SignInButton } from "@clerk/nextjs";
import type { ReactNode } from "react";

/** Opens sign-in (or sign-up) and lands the visitor in the workbench. */
export default function StartButton({
  children,
  large = false,
}: {
  children: ReactNode;
  large?: boolean;
}) {
  return (
    <SignInButton mode="modal" forceRedirectUrl="/briefs" signUpForceRedirectUrl="/briefs">
      <button
        type="button"
        className={`button-primary ${large ? "min-h-[50px] px-[26px] text-[12.5px]" : ""}`}
      >
        {children}
      </button>
    </SignInButton>
  );
}
