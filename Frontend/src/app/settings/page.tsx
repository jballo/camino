"use client";

import { GitHub } from "@/icons/Github";
import { Show, SignInButton } from "@clerk/nextjs";
import {
  Button,
  Description,
  Dialog,
  DialogBackdrop,
  DialogPanel,
  DialogTitle,
  Field,
  Input,
  Label,
} from "@headlessui/react";
import { AlertTriangle, CheckCircle2, Loader2, Trash2 } from "lucide-react";
import { useCallback, useEffect, useState } from "react";

type ConnectionStatus = {
  connected: boolean;
  githubUsername?: string | null;
};

const DELETE_CONFIRMATION = "Delete my account";

export default function Settings() {
  const [status, setStatus] = useState<ConnectionStatus | undefined>(undefined);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | undefined>(undefined);
  const [deleteDialogOpen, setDeleteDialogOpen] = useState(false);
  const [deleteConfirmation, setDeleteConfirmation] = useState("");
  const [returnStatus, setReturnStatus] = useState<
    "success" | "error" | undefined
  >(undefined);

  const closeDeleteDialog = () => {
    setDeleteDialogOpen(false);
    setDeleteConfirmation("");
  };

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
    <div className="flex w-full flex-col min-h-full">
      <div className="mx-auto flex w-full max-w-2xl flex-1 flex-col gap-6 px-8 py-12">
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

          <div className="flex flex-col gap-4 rounded-xl border border-destructive/30 p-6">
            <div className="flex items-start justify-between gap-6">
              <div className="flex flex-col gap-1">
                <h2 className="text-sm font-medium">Delete account</h2>
                <p className="text-xs text-muted-foreground">
                  Permanently remove your Camino account and its associated
                  data.
                </p>
              </div>
              <Button
                onClick={() => setDeleteDialogOpen(true)}
                className="inline-flex shrink-0 items-center gap-2 rounded-md border border-destructive/50 px-3 py-1.5 text-sm text-destructive transition hover:bg-destructive/10"
              >
                <Trash2 className="size-4" />
                Delete account
              </Button>
            </div>
          </div>

          <Dialog
            open={deleteDialogOpen}
            onClose={closeDeleteDialog}
            className="relative z-50"
          >
            <DialogBackdrop className="fixed inset-0 bg-foreground/30 backdrop-blur-[1px]" />
            <div className="fixed inset-0 flex w-screen items-center justify-center p-4">
              <DialogPanel className="w-full max-w-md rounded-xl border border-border bg-background p-6 shadow-xl">
                <div className="flex size-10 items-center justify-center rounded-full bg-destructive/10 text-destructive">
                  <AlertTriangle className="size-5" />
                </div>
                <DialogTitle className="mt-4 text-lg font-semibold">
                  Delete your account?
                </DialogTitle>
                <Description className="mt-2 text-sm text-muted-foreground">
                  This action is permanent. Complete both steps in order to
                  continue.
                </Description>

                <ol className="mt-5 list-decimal space-y-4 pl-5 text-sm">
                  <li>
                    <span>Type this exact phrase:</span>
                    <code className="mt-2 block select-all rounded-md border border-destructive/30 bg-destructive/10 px-3 py-2 font-mono text-base font-semibold text-foreground">
                      {DELETE_CONFIRMATION}
                    </code>
                  </li>
                  <li>Click the delete button to confirm.</li>
                </ol>

                <Field className="mt-5">
                  <Label className="text-sm font-medium">
                    Enter the phrase shown above
                  </Label>
                  <Input
                    autoFocus
                    value={deleteConfirmation}
                    onChange={(event) =>
                      setDeleteConfirmation(event.target.value)
                    }
                    placeholder="Type the exact phrase"
                    autoComplete="off"
                    className="mt-2 w-full rounded-md border border-input bg-background px-3 py-2 text-sm outline-none transition placeholder:text-muted-foreground focus:border-ring focus:ring-2 focus:ring-ring/20"
                  />
                </Field>

                <div className="mt-6 flex justify-end gap-3">
                  <Button
                    onClick={closeDeleteDialog}
                    className="rounded-md border border-border px-4 py-2 text-sm transition hover:bg-accent"
                  >
                    Cancel
                  </Button>
                  <Button
                    disabled={deleteConfirmation !== DELETE_CONFIRMATION}
                    onClick={closeDeleteDialog}
                    className="rounded-md bg-destructive px-4 py-2 text-sm text-destructive-foreground transition hover:opacity-90 disabled:cursor-not-allowed disabled:bg-muted disabled:text-muted-foreground disabled:opacity-100"
                  >
                    Delete account
                  </Button>
                </div>
              </DialogPanel>
            </div>
          </Dialog>
        </Show>
      </div>
    </div>
  );
}
