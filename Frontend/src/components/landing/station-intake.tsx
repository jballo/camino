"use client";

import { useClerk } from "@clerk/nextjs";
import { useId, useState } from "react";

import { parseIssueUrl } from "@/lib/issue-url";

/**
 * Station 1's field. A valid issue URL opens sign-in, which lands the visitor
 * on /briefs with the issue filled in and its read-only preview running.
 */
export default function StationIntake({ placeholder }: { placeholder: string }) {
  const { openSignIn } = useClerk();
  const [value, setValue] = useState("");
  const [invalid, setInvalid] = useState(false);
  const inputId = useId();
  const errorId = useId();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const issueUrl = parseIssueUrl(value);
    if (!issueUrl) {
      setInvalid(true);
      return;
    }
    const destination = `/briefs?issue=${encodeURIComponent(issueUrl)}`;
    openSignIn({ forceRedirectUrl: destination, signUpForceRedirectUrl: destination });
  }

  return (
    <form onSubmit={submit} noValidate className="flex flex-col gap-2">
      <div className="flex flex-col gap-[9px] sm:flex-row sm:items-center">
        <label htmlFor={inputId} className="sr-only">
          GitHub issue URL
        </label>
        <input
          id={inputId}
          type="url"
          inputMode="url"
          autoComplete="off"
          spellCheck={false}
          value={value}
          onChange={(event) => {
            setValue(event.target.value);
            if (invalid) setInvalid(false);
          }}
          placeholder={placeholder}
          aria-invalid={invalid || undefined}
          aria-describedby={invalid ? errorId : undefined}
          className="field-control min-h-[42.25px] min-w-0 flex-1 px-3 font-mono text-[10.5px] placeholder:text-muted-foreground"
        />
        <button type="submit" className="button-primary min-h-[42.25px] px-4 tracking-[.09em]">
          Preview
        </button>
      </div>
      {invalid ? (
        <p id={errorId} role="alert" className="font-mono text-[10px] text-destructive">
          That isn&apos;t a GitHub issue link. It should look like the example.
        </p>
      ) : (
        <p className="font-mono text-[10px] text-muted-foreground">
          You&apos;ll sign in first, then the preview runs.
        </p>
      )}
    </form>
  );
}
