"use client";

import { Button, Textarea } from "@headlessui/react";
import { useAuth } from "@clerk/nextjs";
import { ArrowUp, CircleCheck, Loader2, Search } from "lucide-react";
import {
  FormEvent,
  KeyboardEvent,
  useCallback,
  useEffect,
  useRef,
  useState,
} from "react";
import ReactMarkdown from "react-markdown";

import { ApiError, backendFetch } from "@/lib/api";
import {
  answerMatchesSelection,
  repositorySelectionChanged,
} from "./state";

type Source = {
  chunk_id: number;
  repo_name: string;
  file_path: string;
  symbol_name: string;
  symbol_type: string;
  language: string;
  start_line: number;
  end_line: number;
  score: number;
};

type AgentAnswer = { answer: string; sources: Source[] };

type DisplayedAnswer = AgentAnswer & { repoName: string; ref: string };

type RepoRef = {
  ref: string;
  chunkCount: number;
  indexedSha: string | null;
  indexedAt: string | null;
};

type RepoEntry = {
  repoName: string;
  refs: RepoRef[];
  transient?: boolean;
};

type RepoOverview = {
  installed: RepoEntry[];
  requested: RepoEntry[];
};

type RepoLookup = {
  repoName: string;
  visibility: string;
  indexed: boolean;
  followed: boolean;
  refs: RepoRef[];
};

type RepoFollowResult = {
  repoName: string;
  followed: boolean;
  indexed: boolean;
  jobQueued: boolean;
};

type ActiveTab = "installed" | "requested";

type LookupState =
  | { status: "idle" }
  | { status: "checking" }
  | { status: "found"; data: RepoLookup }
  | { status: "notFound"; data: RepoLookup }
  | { status: "error"; message: string };

const EMPTY_OVERVIEW: RepoOverview = { installed: [], requested: [] };
const REPOSITORY_NAME = /^[A-Za-z0-9_.-]+\/[A-Za-z0-9_.-]+$/;

function normalizeRepoName(repoName: string) {
  return repoName.trim().toLocaleLowerCase();
}

function relativeTime(value: string | null) {
  if (!value) return "unknown";
  const timestamp = new Date(value).getTime();
  if (!Number.isFinite(timestamp)) return "unknown";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "just now";
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.floor(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.floor(months / 12)}y ago`;
}

function shortSha(sha: string | null) {
  return sha ? sha.slice(0, 7) : "—";
}

function repoMeta(repo: RepoEntry, tab: ActiveTab) {
  if (repo.transient) return "not in your list yet";
  if (repo.refs.length === 0) {
    return tab === "installed"
      ? "no index yet — select to request"
      : "requested — not indexed yet";
  }
  const chunks = repo.refs.reduce((sum, item) => sum + item.chunkCount, 0);
  const newest = [...repo.refs]
    .filter((item) => item.indexedAt)
    .sort(
      (a, b) =>
        new Date(b.indexedAt ?? 0).getTime() -
        new Date(a.indexedAt ?? 0).getTime(),
    )[0];
  const refLabel = repo.refs.length === 1 ? "ref" : "refs";
  return `${repo.refs.length} ${refLabel} · ${chunks.toLocaleString()} chunks · ${relativeTime(newest?.indexedAt ?? null)}`;
}

export default function Explore() {
  const { getToken } = useAuth();
  const [overview, setOverview] = useState<RepoOverview>(EMPTY_OVERVIEW);
  const [overviewLoading, setOverviewLoading] = useState(true);
  const [overviewError, setOverviewError] = useState<string>();
  const [activeTab, setActiveTab] = useState<ActiveTab>("installed");
  const [selectedRepo, setSelectedRepo] = useState<string>();
  const [selectedRefs, setSelectedRefs] = useState<Record<string, string>>({});

  const [repoInput, setRepoInput] = useState("");
  const [lookup, setLookup] = useState<LookupState>({ status: "idle" });
  const [addingRepo, setAddingRepo] = useState(false);
  const [toast, setToast] = useState<string>();

  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string>();
  const [answer, setAnswer] = useState<DisplayedAnswer>();
  const tabRefs = useRef<Array<HTMLButtonElement | null>>([]);
  const loadedOnce = useRef(false);
  const previousRepo = useRef<string | undefined>(undefined);
  const askAbortRef = useRef<AbortController | null>(null);

  const loadOverview = useCallback(
    async (preferredRepo?: string) => {
      setOverviewLoading(true);
      setOverviewError(undefined);
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");
        const result = await backendFetch<RepoOverview>(
          "/api/v1/repositories/overview",
          token,
        );
        const next: RepoOverview = {
          installed: Array.isArray(result.installed) ? result.installed : [],
          requested: Array.isArray(result.requested) ? result.requested : [],
        };
        const normalizedPreferred = preferredRepo
          ? normalizeRepoName(preferredRepo)
          : undefined;
        const preferredInstalled = next.installed.some(
          (repo) => repo.repoName === normalizedPreferred,
        );
        const preferredRequested = next.requested.some(
          (repo) => repo.repoName === normalizedPreferred,
        );
        if (normalizedPreferred && !preferredInstalled && !preferredRequested) {
          let transientRefs: RepoRef[] = [];
          try {
            const lookupResult = await backendFetch<RepoLookup>(
              `/api/v1/repositories/lookup?repoName=${encodeURIComponent(normalizedPreferred)}`,
              token,
            );
            transientRefs = lookupResult.refs;
          } catch {
            // Keep the deep-link row visible; the detail panel will offer a
            // request path if the repository is accessible but not indexed.
          }
          next.requested = [
            {
              repoName: normalizedPreferred,
              refs: transientRefs,
              transient: true,
            },
            ...next.requested,
          ];
        }

        setOverview(next);
        setSelectedRepo((current) => {
          if (normalizedPreferred) return normalizedPreferred;
          const stillPresent = [...next.installed, ...next.requested].some(
            (repo) => repo.repoName === current,
          );
          if (stillPresent) return current;
          return next.requested[0]?.repoName ?? next.installed[0]?.repoName;
        });
        setActiveTab((current) => {
          if (preferredInstalled) return "installed";
          if (normalizedPreferred) return "requested";
          if (!loadedOnce.current) {
            return next.requested.length > 0 ? "requested" : "installed";
          }
          return current === "requested" && next.requested.length === 0
            ? "installed"
            : current;
        });
        loadedOnce.current = true;
      } catch (error) {
        console.error("Failed to load repository overview", error);
        setOverviewError("Could not load your repositories.");
      } finally {
        setOverviewLoading(false);
      }
    },
    [getToken],
  );

  useEffect(() => {
    const params = new URLSearchParams(window.location.search);
    const initialRepo = params.get("repo") ?? undefined;
    const initialQuestion = params.get("question");
    if (initialQuestion) setQuery(initialQuestion);
    void loadOverview(initialRepo);
  }, [loadOverview]);

  useEffect(() => {
    if (repositorySelectionChanged(previousRepo.current, selectedRepo)) {
      askAbortRef.current?.abort();
      askAbortRef.current = null;
      setQuery("");
      setAnswer(undefined);
      setAskError(undefined);
      setAsking(false);
    }
    previousRepo.current = selectedRepo;
  }, [selectedRepo]);

  useEffect(
    () => () => {
      askAbortRef.current?.abort();
    },
    [],
  );

  useEffect(() => {
    const value = repoInput.trim();
    if (!value) {
      setLookup({ status: "idle" });
      return;
    }
    if (!REPOSITORY_NAME.test(value)) {
      setLookup({ status: "error", message: "Use the owner/repo format." });
      return;
    }

    const controller = new AbortController();
    const timeout = window.setTimeout(async () => {
      setLookup({ status: "checking" });
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");
        const result = await backendFetch<RepoLookup>(
          `/api/v1/repositories/lookup?repoName=${encodeURIComponent(value)}`,
          token,
          { signal: controller.signal },
        );
        setLookup({
          status: result.indexed ? "found" : "notFound",
          data: result,
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error instanceof ApiError && error.status === 404) {
          setLookup({
            status: "error",
            message: "Not found — or private (install the GitHub App).",
          });
        } else if (error instanceof ApiError && error.status === 502) {
          setLookup({
            status: "error",
            message: "GitHub check failed — try again.",
          });
        } else {
          setLookup({
            status: "error",
            message: "Could not check that repository.",
          });
        }
      }
    }, 400);

    return () => {
      window.clearTimeout(timeout);
      controller.abort();
    };
  }, [getToken, repoInput]);

  const selectedEntry = [...overview.installed, ...overview.requested].find(
    (repo) => repo.repoName === selectedRepo,
  );
  const selectedRepoRefs = selectedEntry?.refs ?? [];
  const selectedRef = selectedRepoRefs.some(
    (item) => item.ref === selectedRefs[selectedRepo ?? ""],
  )
    ? selectedRefs[selectedRepo ?? ""]
    : selectedRepoRefs[0]?.ref;
  const activeRef = selectedRepoRefs.find((item) => item.ref === selectedRef);

  const followRepository = useCallback(
    async (repoName: string, lookupData?: RepoLookup) => {
      setAddingRepo(true);
      setAskError(undefined);
      try {
        const token = await getToken();
        if (!token) throw new ApiError(401, "Not authenticated");
        const result = await backendFetch<RepoFollowResult>(
          "/api/v1/repositories/follows",
          token,
          { method: "POST", body: { repoName } },
        );
        const inInstalled = overview.installed.some(
          (repo) => repo.repoName === result.repoName,
        );
        setToast(
          result.indexed
            ? `${result.repoName} added to ${inInstalled ? "Installed" : "Requested"}.`
            : `${result.repoName} queued. It appears above with full detail once indexed — no need to wait around.`,
        );
        if (result.indexed && lookupData && !inInstalled) {
          setOverview((current) => ({
            ...current,
            requested: [
              { repoName: result.repoName, refs: lookupData.refs },
              ...current.requested.filter(
                (repo) => repo.repoName !== result.repoName,
              ),
            ],
          }));
        }
        setSelectedRepo(result.repoName);
        setActiveTab(inInstalled ? "installed" : "requested");
        setRepoInput("");
        setLookup({ status: "idle" });
        await loadOverview(result.repoName);
        return true;
      } catch (error) {
        console.error("Failed to follow repository", error);
        const message =
          error instanceof ApiError && error.status === 404
            ? "Repository not found — or it is private and unavailable to the GitHub App."
            : error instanceof ApiError && error.status === 502
              ? "GitHub check failed — try again."
              : "Could not add that repository.";
        setLookup({ status: "error", message });
        setAskError(message);
        return false;
      } finally {
        setAddingRepo(false);
      }
    },
    [getToken, loadOverview, overview.installed],
  );

  const submitRepo = (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (lookup.status !== "found" && lookup.status !== "notFound") return;
    void followRepository(lookup.data.repoName, lookup.data);
  };

  const askAgent = useCallback(async () => {
    if (!selectedRepo || !selectedRef || query.trim().length === 0) return;
    askAbortRef.current?.abort();
    const controller = new AbortController();
    askAbortRef.current = controller;
    setAsking(true);
    setAskError(undefined);
    setAnswer(undefined);
    try {
      const token = await getToken();
      if (!token) throw new ApiError(401, "Not authenticated");
      const result = await backendFetch<AgentAnswer>(
        "/api/v1/agent/ask",
        token,
        {
          method: "POST",
          body: { question: query, repoName: selectedRepo, ref: selectedRef },
          signal: controller.signal,
        },
      );
      setAnswer({ ...result, repoName: selectedRepo, ref: selectedRef });
      if (selectedEntry?.transient) {
        void backendFetch<RepoFollowResult>(
          "/api/v1/repositories/follows",
          token,
          { method: "POST", body: { repoName: selectedRepo } },
        )
          .then(() => loadOverview(selectedRepo))
          .catch((error) => {
            console.error("Failed to save deep-linked repository", error);
          });
      }
    } catch (error) {
      if (controller.signal.aborted) return;
      console.error("Failed to get an answer", error);
      setAskError("Failed to get an answer.");
    } finally {
      if (askAbortRef.current === controller) {
        askAbortRef.current = null;
        setAsking(false);
      }
    }
  }, [
    getToken,
    loadOverview,
    query,
    selectedEntry?.transient,
    selectedRef,
    selectedRepo,
  ]);

  const visibleRepos = overview[activeTab];
  const lookupHint = (() => {
    if (lookup.status === "checking") return "Checking shared index…";
    if (lookup.status === "found") {
      const firstRef = lookup.data.refs[0];
      return `Already indexed · ${firstRef?.ref ?? "ref available"} · ${(firstRef?.chunkCount ?? 0).toLocaleString()} chunks — ⏎ add`;
    }
    if (lookup.status === "notFound") {
      return "Not indexed — ⏎ request index";
    }
    if (lookup.status === "error") return lookup.message;
    return "";
  })();

  const changeTab = (tab: ActiveTab) => {
    setActiveTab(tab);
    const firstRepo = overview[tab][0];
    setSelectedRepo(firstRepo?.repoName);
  };

  const handleTabKeyDown = (
    event: KeyboardEvent<HTMLButtonElement>,
    index: number,
  ) => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    event.preventDefault();
    const nextIndex = index === 0 ? 1 : 0;
    const nextTab: ActiveTab = nextIndex === 0 ? "installed" : "requested";
    changeTab(nextTab);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <div className="mx-auto flex min-h-full w-full max-w-[1400px] flex-col gap-6 px-5 py-10 sm:px-8 lg:flex-row lg:items-start">
      <aside className="console flex w-full shrink-0 flex-col lg:min-h-[560px] lg:w-[330px]">
        <div
          className="flex border-b border-border"
          role="tablist"
          aria-label="Repository groups"
        >
          {(["installed", "requested"] as const).map((tab, index) => (
            <button
              key={tab}
              ref={(node) => {
                tabRefs.current[index] = node;
              }}
              id={`${tab}-tab`}
              type="button"
              role="tab"
              aria-selected={activeTab === tab}
              aria-controls="repository-list-panel"
              tabIndex={activeTab === tab ? 0 : -1}
              onClick={() => changeTab(tab)}
              onKeyDown={(event) => handleTabKeyDown(event, index)}
              className={`flex min-h-12 flex-1 items-center justify-center gap-2 border-b-2 font-mono text-[10.5px] uppercase tracking-[.14em] transition ${
                activeTab === tab
                  ? "border-brand-accent bg-muted text-foreground"
                  : "border-transparent text-muted-foreground hover:bg-muted/60 hover:text-foreground"
              }`}
            >
              {tab}
              <span className="text-brand-accent">{overview[tab].length}</span>
            </button>
          ))}
        </div>

        <div
          id="repository-list-panel"
          role="tabpanel"
          aria-labelledby={`${activeTab}-tab`}
          className="flex flex-1 flex-col"
        >
          <div className="flex flex-col">
            {overviewLoading && visibleRepos.length === 0 && (
              <div className="flex items-center gap-2 px-4 py-5 font-mono text-[11px] text-muted-foreground">
                <Loader2
                  className="size-3.5 animate-spin"
                  aria-hidden="true"
                />
                Loading repositories…
              </div>
            )}
            {overviewError && (
              <div className="space-y-3 px-4 py-5 text-sm text-destructive">
                <p>{overviewError}</p>
                <button
                  type="button"
                  className="button-ghost min-h-9 px-4"
                  onClick={() => void loadOverview(selectedRepo)}
                >
                  Try again
                </button>
              </div>
            )}
            {!overviewLoading &&
              !overviewError &&
              visibleRepos.length === 0 && (
                <div className="px-4 py-5 text-sm leading-relaxed text-muted-foreground">
                  {activeTab === "installed"
                    ? "No installed repositories found. Connect or update the GitHub App first."
                    : "Repositories you request will appear here."}
                </div>
              )}
            {visibleRepos.map((repo) => {
              const selected = repo.repoName === selectedRepo;
              return (
                <button
                  key={repo.repoName}
                  type="button"
                  onClick={() => setSelectedRepo(repo.repoName)}
                  className={`flex w-full flex-col gap-1 border-b border-border px-4 py-3 text-left transition hover:bg-muted ${
                    selected
                      ? "bg-muted shadow-[inset_3px_0_0_var(--brand-accent)]"
                      : ""
                  }`}
                  aria-pressed={selected}
                >
                  <span
                    className={
                      repo.refs.length === 0
                        ? "text-muted-foreground"
                        : "text-foreground"
                    }
                  >
                    {repo.repoName}
                  </span>
                  <span className="font-mono text-[10.5px] leading-relaxed tracking-[.04em] text-muted-foreground">
                    {repoMeta(repo, activeTab)}
                  </span>
                </button>
              );
            })}
          </div>

          <form
            onSubmit={submitRepo}
            className="mt-auto flex flex-col gap-2.5 border-t border-border bg-background p-4"
          >
            <label htmlFor="repo-lookup" className="field-label">
              Find or index — owner/repo
            </label>
            <input
              id="repo-lookup"
              value={repoInput}
              onChange={(event) => {
                setRepoInput(event.target.value);
                setToast(undefined);
              }}
              placeholder="e.g. django/django"
              autoComplete="off"
              spellCheck={false}
              className="field-control min-h-[42px] w-full font-mono text-xs"
            />
            {lookupHint && (
              <p
                className={`font-mono text-[10.5px] leading-relaxed tracking-[.03em] ${
                  lookup.status === "error"
                    ? "text-destructive"
                    : "text-muted-foreground"
                }`}
                aria-live="polite"
              >
                {lookupHint}
              </p>
            )}
            <button
              type="submit"
              className="sr-only"
              disabled={
                addingRepo ||
                (lookup.status !== "found" && lookup.status !== "notFound")
              }
            >
              Add repository
            </button>
            {toast && (
              <div
                role="status"
                className="flex items-start gap-2.5 rounded-[10px] border border-success p-3"
              >
                <CircleCheck
                  className="mt-0.5 size-4 shrink-0 text-success"
                  aria-hidden="true"
                />
                <span className="font-mono text-[10.5px] leading-relaxed tracking-[.03em]">
                  {toast}
                </span>
              </div>
            )}
          </form>
        </div>
      </aside>

      <main className="flex min-w-0 flex-1 flex-col gap-5">
        <header>
          <span className="eyebrow">Grounded code search</span>
          <h1 className="display-title mt-2 text-4xl font-black sm:text-[42px]">
            Ask the codebase<span className="text-brand-accent">.</span>
          </h1>
        </header>

        {selectedEntry && selectedRepoRefs.length > 0 && activeRef && (
          <section className="console" aria-labelledby="index-detail-title">
            <div className="console-bar gap-4">
              <span
                className="truncate text-foreground"
                title={selectedEntry.repoName}
              >
                {selectedEntry.repoName}
              </span>
              <span id="index-detail-title" className="shrink-0">
                Index detail
              </span>
            </div>
            <div className="grid grid-cols-2 border-b border-border lg:grid-cols-4">
              <DetailCell
                label="Refs"
                value={selectedRepoRefs.length.toString()}
              />
              <DetailCell
                label="Chunks"
                value={activeRef.chunkCount.toLocaleString()}
              />
              <DetailCell
                label="Indexed SHA"
                value={shortSha(activeRef.indexedSha)}
              />
              <DetailCell
                label="Indexed"
                value={relativeTime(activeRef.indexedAt)}
              />
            </div>
            <div className="overflow-x-auto">
              <table className="w-full min-w-[560px] border-collapse">
                <thead>
                  <tr>
                    {["Ref", "Chunks", "SHA", "Indexed"].map((label) => (
                      <th
                        key={label}
                        scope="col"
                        className="px-[18px] py-2.5 text-left font-mono text-[9.5px] uppercase tracking-[.14em] text-muted-foreground"
                      >
                        {label}
                      </th>
                    ))}
                  </tr>
                </thead>
                <tbody>
                  {selectedRepoRefs.map((item) => {
                    const isActive = item.ref === selectedRef;
                    return (
                      <tr
                        key={item.ref}
                        onClick={() =>
                          setSelectedRefs((current) => ({
                            ...current,
                            [selectedEntry.repoName]: item.ref,
                          }))
                        }
                        className="cursor-pointer border-t border-border transition hover:bg-muted/60"
                        aria-selected={isActive}
                      >
                        <td
                          className={`px-[18px] py-2.5 font-mono text-[11px] ${
                            isActive ? "text-brand-accent" : ""
                          }`}
                        >
                          <button
                            type="button"
                            className="w-full text-left"
                            aria-label={`Use ref ${item.ref}`}
                          >
                            {item.ref}{" "}
                            {isActive && <span aria-hidden="true">●</span>}
                          </button>
                        </td>
                        <td className="px-[18px] py-2.5 font-mono text-[11px]">
                          {item.chunkCount.toLocaleString()}
                        </td>
                        <td className="px-[18px] py-2.5 font-mono text-[11px]">
                          {shortSha(item.indexedSha)}
                        </td>
                        <td className="px-[18px] py-2.5 font-mono text-[11px] text-muted-foreground">
                          {relativeTime(item.indexedAt)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <p className="border-t border-border px-[18px] py-3 font-mono text-[10px] leading-relaxed tracking-[.05em] text-muted-foreground">
              Shared index — anyone on Camino may refresh it. You always query
              the latest completed build.
            </p>
          </section>
        )}

        {selectedEntry ? (
          <section className="console" aria-labelledby="ask-title">
            <div className="console-bar gap-4">
              <span id="ask-title">Ask</span>
              <span className="truncate">
                {selectedRef ? `Ref · ${selectedRef}` : "Index required"}
              </span>
            </div>
            {selectedRepoRefs.length > 0 ? (
              <div className="flex flex-col gap-3.5 p-[18px]">
                <label htmlFor="code-question" className="sr-only">
                  Question about {selectedEntry.repoName}
                </label>
                <Textarea
                  id="code-question"
                  placeholder={`Ask a question about ${selectedEntry.repoName}…`}
                  className="min-h-16 max-h-48 w-full resize-none bg-transparent text-start outline-none"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  onKeyDown={(event) => {
                    if (
                      event.key === "Enter" &&
                      (event.metaKey || event.ctrlKey)
                    ) {
                      event.preventDefault();
                      void askAgent();
                    }
                  }}
                />
                <div className="flex items-center justify-between gap-4">
                  <span className="font-mono text-[10.5px] tracking-[.06em] text-muted-foreground">
                    ⌘⏎ to ask
                  </span>
                  <Button
                    onClick={() => void askAgent()}
                    disabled={asking || query.trim().length === 0}
                    className="button-primary min-h-11 px-6"
                  >
                    {asking ? (
                      <Loader2
                        className="size-4 animate-spin"
                        aria-hidden="true"
                      />
                    ) : (
                      <ArrowUp className="size-4" aria-hidden="true" />
                    )}
                    Ask
                  </Button>
                </div>
              </div>
            ) : (
              <div className="flex flex-col items-start gap-4 p-5 sm:p-6">
                <div>
                  <h2 className="text-lg font-medium">
                    {selectedEntry.repoName} isn&apos;t indexed yet
                  </h2>
                  <p className="mt-1 max-w-xl text-sm leading-relaxed text-muted-foreground">
                    Request a shared index, then come back later. You do not
                    need to keep this page open.
                  </p>
                </div>
                <Button
                  onClick={() =>
                    void followRepository(selectedEntry.repoName)
                  }
                  disabled={addingRepo}
                  className="button-primary"
                >
                  {addingRepo && (
                    <Loader2
                      className="size-4 animate-spin"
                      aria-hidden="true"
                    />
                  )}
                  Request index
                </Button>
              </div>
            )}
          </section>
        ) : (
          !overviewLoading && (
            <div className="console flex flex-col items-center justify-center gap-3 px-6 py-16 text-center text-muted-foreground">
              <Search className="size-7" aria-hidden="true" />
              <p className="max-w-sm text-sm leading-relaxed">
                Choose a repository from the rail or find a public repository
                by owner and name.
              </p>
            </div>
          )
        )}

        {askError && (
          <p role="alert" className="text-sm text-destructive">
            {askError}
          </p>
        )}

        {answerMatchesSelection(answer, selectedRepo, selectedRef) && answer && (
            <div className="flex flex-col gap-4">
              <section
                className="console p-5 sm:p-6"
                aria-labelledby="answer-title"
              >
                <div
                  id="answer-title"
                  className="mb-3 font-mono text-[11px] uppercase tracking-[.14em] text-muted-foreground"
                >
                  Answer ·{" "}
                  <span className="text-foreground">{selectedRepo}</span>
                </div>
                <div className="text-sm leading-relaxed [&_a]:text-brand-accent [&_a]:underline [&_code]:rounded [&_code]:bg-accent [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-xs [&_li:not(:last-child)]:mb-1 [&_ol]:mb-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_p:not(:last-child)]:mb-3 [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:border [&_pre]:border-border [&_pre]:bg-muted [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_strong]:font-semibold [&_ul]:mb-3 [&_ul]:list-disc [&_ul]:pl-5">
                  <ReactMarkdown>{answer.answer}</ReactMarkdown>
                </div>
              </section>

              {answer.sources.length > 0 && (
                <section
                  className="flex flex-col gap-3"
                  aria-labelledby="sources-title"
                >
                  <h2 id="sources-title" className="eyebrow">
                    {answer.sources.length}{" "}
                    {answer.sources.length === 1 ? "source" : "sources"}
                  </h2>
                  {answer.sources.map((source) => (
                    <SourceCard key={source.chunk_id} source={source} />
                  ))}
                </section>
              )}
            </div>
          )}
      </main>
    </div>
  );
}

function DetailCell({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex min-w-0 flex-col gap-1 border-b border-r border-border p-3.5 last:border-r-0 even:border-r-0 lg:even:border-r lg:last:border-r-0">
      <span className="font-mono text-[9.5px] uppercase tracking-[.14em] text-muted-foreground">
        {label}
      </span>
      <span
        className="truncate font-mono text-[13px] text-foreground"
        title={value}
      >
        {value}
      </span>
    </div>
  );
}

function SourceCard({ source }: { source: Source }) {
  return (
    <article className="rounded-xl border border-border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className="truncate font-mono text-sm">
              {source.symbol_name}
            </span>
            <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-xs text-muted-foreground">
              {source.symbol_type}
            </span>
            <span className="shrink-0 rounded bg-accent px-1.5 py-0.5 text-xs text-muted-foreground">
              {source.language}
            </span>
          </div>
          <div className="truncate text-xs text-muted-foreground">
            {source.file_path}:{source.start_line}-{source.end_line}
          </div>
        </div>
        <div className="shrink-0 font-mono text-xs text-brand-accent">
          {source.score.toFixed(3)}
        </div>
      </div>
    </article>
  );
}
