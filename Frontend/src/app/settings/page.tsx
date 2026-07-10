"use client";

import Header from "@/components/header";
import { GitHub } from "@/icons/Github";
import { Show, SignInButton } from "@clerk/nextjs";
import { AlertTriangle, CheckCircle2, Loader2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

type ConnectionStatus = {
  connected: boolean;
  githubUsername?: string | null;
};

export default function Settings() {
  const [status, setStatus] = useState<ConnectionStatus | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>(undefined);
  const [returnStatus, setReturnStatus] = useState<
    "success" | "error" | undefined
  >(undefined);

  const loadStatus = useCallback(async () => {
    setLoading(true);
    setError(undefined);
    try {
      const response = await fetch("/api/github/status", { method: "GET" });
      const body = (await response.json().catch(() => null)) as
        | (ConnectionStatus & { error?: string; detail?: string })
        | null;

      if (!response.ok) {
        if (response.status === 401) {
          setError("Your session expired. Refresh the page and sign in again.");
        } else {
          setError(
            body?.error
              ? `Couldn't check your GitHub connection — ${body.error}.`
              : "Couldn't check your GitHub connection.",
          );
        }
        return;
      }

      setStatus(body ?? { connected: false });
    } catch (err) {
      console.log("Error: ", err);
      setError("Couldn't reach the app to check your GitHub connection.");
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    loadStatus();
  }, [loadStatus]);

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const github = params.get("github");
    if (github) {
      setReturnStatus(github === "error" ? "error" : "success");
      // Clean the param out of the URL without a reload.
      window.history.replaceState(null, "", "/settings");
    }
  }, []);

  return (
    <div className="flex w-full flex-col min-h-screen">
      <Header />
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-8 pb-16">
        <div className="flex flex-col gap-1">
          <h1 className="text-2xl font-semibold">Settings</h1>
          <p className="text-sm text-muted-foreground">
            Manage the connections Camino uses to read your code.
          </p>
        </div>

        <Show when="signed-out">
          <div className="flex flex-col items-start gap-3 rounded-xl border border-border p-6">
            <p className="text-sm text-muted-foreground">
              Sign in to manage your GitHub connection.
            </p>
            <SignInButton mode="modal">
              <button className="rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground transition hover:opacity-90">
                Sign in
              </button>
            </SignInButton>
          </div>
        </Show>

        <Show when="signed-in">
          {returnStatus === "success" && (
            <div className="flex items-center gap-2 rounded-lg border border-primary/40 bg-primary/10 px-4 py-3 text-sm text-primary">
              <CheckCircle2 className="size-4 shrink-0" />
              GitHub updated. Your repository access is now in sync.
            </div>
          )}
          {returnStatus === "error" && (
            <div className="flex items-center gap-2 rounded-lg border border-destructive/40 bg-destructive/10 px-4 py-3 text-sm text-destructive">
              <AlertTriangle className="size-4 shrink-0" />
              Something went wrong finishing the GitHub connection. Please try
              again.
            </div>
          )}
          <div className="flex flex-col gap-4 rounded-xl border border-border p-6">
            <div className="flex items-start justify-between gap-4">
              <div className="flex items-center gap-3">
                <span className="flex size-10 items-center justify-center rounded-lg bg-accent">
                  <GitHub className="size-5" />
                </span>
                <div className="flex flex-col">
                  <span className="text-sm font-medium">GitHub</span>
                  <span className="text-xs text-muted-foreground">
                    Connect a repository to generate tours and ask questions.
                  </span>
                </div>
              </div>
            </div>

            <div className="border-t border-border pt-4">
              {loading ? (
                <div className="flex items-center gap-2 text-sm text-muted-foreground">
                  <Loader2 className="size-4 animate-spin" />
                  Checking connection…
                </div>
              ) : error ? (
                <div className="flex items-center justify-between gap-4">
                  <span className="text-sm text-destructive">{error}</span>
                  <button
                    onClick={loadStatus}
                    className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
                  >
                    Retry
                  </button>
                </div>
              ) : status?.connected ? (
                <div className="flex items-center justify-between gap-4">
                  <span className="inline-flex items-center gap-1.5 text-sm text-primary">
                    <CheckCircle2 className="size-4" />
                    Connected
                    {status.githubUsername ? (
                      <span className="text-muted-foreground">
                        as{" "}
                        <span className="font-mono text-foreground">
                          @{status.githubUsername}
                        </span>
                      </span>
                    ) : null}
                  </span>
                  <a
                    href="/api/github/install"
                    className="rounded-md border border-border px-3 py-1.5 text-sm hover:bg-accent"
                  >
                    Manage repositories
                  </a>
                </div>
              ) : (
                <div className="flex items-center justify-between gap-4">
                  <span className="text-sm text-muted-foreground">
                    Not connected yet.
                  </span>
                  <a
                    href="/api/github/install"
                    className="inline-flex items-center gap-2 rounded-md bg-primary px-4 py-2 text-sm text-primary-foreground transition hover:opacity-90"
                  >
                    <GitHub className="size-4" />
                    Connect GitHub
                  </a>
                </div>
              )}
            </div>
          </div>
        </Show>
      </div>
    </div>
  );
}
