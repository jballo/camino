"use client";

import Header from "@/components/header";
import { Button, Textarea } from "@headlessui/react";
import {
  ArrowUp,
  Check,
  CheckCircle2,
  Circle,
  FileCode,
  Loader2,
  RefreshCw,
  Search,
} from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import ReactMarkdown from "react-markdown";

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

type AgentAnswer = {
  answer: string;
  sources: Source[];
};

type IngestResult = {
  repoName: string;
  chunks_inserted: number;
  embeddings_created: number;
};

export default function Explore() {
  const [repos, setRepos] = useState<string[]>([]);
  const [reposLoading, setReposLoading] = useState(false);
  const [reposError, setReposError] = useState<string | undefined>(undefined);
  const [selectedRepo, setSelectedRepo] = useState<string | undefined>(
    undefined,
  );

  const [processingRepo, setProcessingRepo] = useState<string | undefined>(
    undefined,
  );
  const [ingestResult, setIngestResult] = useState<IngestResult | undefined>(
    undefined,
  );
  const [processError, setProcessError] = useState<string | undefined>(
    undefined,
  );
  const [processedMap, setProcessedMap] = useState<Record<string, number>>({});
  const [processedLoading, setProcessedLoading] = useState(false);

  const [query, setQuery] = useState("");
  const [asking, setAsking] = useState(false);
  const [askError, setAskError] = useState<string | undefined>(undefined);
  const [answer, setAnswer] = useState<AgentAnswer | undefined>(undefined);

  const loadRepos = useCallback(async () => {
    setReposLoading(true);
    setReposError(undefined);
    try {
      const response = await fetch("/api/repositories", { method: "GET" });
      if (!response.ok) throw new Error("Failed to get repos");
      const result = await response.json();
      setRepos(Array.isArray(result) ? result : []);
    } catch (error) {
      console.log("Error: ", error);
      setReposError("Failed to retrieve repositories");
    } finally {
      setReposLoading(false);
    }
  }, []);

  const loadProcessed = useCallback(async () => {
    setProcessedLoading(true);
    try {
      const response = await fetch("/api/repositories/processed", {
        method: "GET",
      });
      if (!response.ok) throw new Error("Failed to get processed repos");
      const result = (await response.json()) as {
        repo_name: string;
        chunk_count: number;
      }[];
      const map: Record<string, number> = {};
      if (Array.isArray(result)) {
        for (const row of result) map[row.repo_name] = row.chunk_count;
      }
      setProcessedMap(map);
    } catch (error) {
      console.log("Error: ", error);
    } finally {
      setProcessedLoading(false);
    }
  }, []);

  useEffect(() => {
    loadRepos();
    loadProcessed();
  }, [loadRepos, loadProcessed]);

  const processRepo = useCallback(
    async (repoName: string) => {
      setProcessingRepo(repoName);
      setIngestResult(undefined);
      setProcessError(undefined);
      try {
        const response = await fetch("/api/repositories/ingest", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ repoName }),
        });
        if (!response.ok) throw new Error("Failed to process repo");
        const result = (await response.json()) as IngestResult;
        setIngestResult(result);
        setProcessedMap((prev) => ({
          ...prev,
          [repoName]: result.chunks_inserted,
        }));
        loadProcessed();
      } catch (error) {
        console.log("Error: ", error);
        setProcessError(`Failed to process ${repoName}`);
      } finally {
        setProcessingRepo(undefined);
      }
    },
    [loadProcessed],
  );

  const askAgent = useCallback(async () => {
    if (!selectedRepo || query.trim().length === 0) return;
    setAsking(true);
    setAskError(undefined);
    setAnswer(undefined);
    try {
      const response = await fetch("/api/agent/ask", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: query, repoName: selectedRepo }),
      });
      if (!response.ok) throw new Error("Failed to ask agent");
      const result = (await response.json()) as AgentAnswer;
      setAnswer(result);
    } catch (error) {
      console.log("Error: ", error);
      setAskError("Failed to get an answer");
    } finally {
      setAsking(false);
    }
  }, [selectedRepo, query]);

  return (
    <div className="flex flex-col w-full min-h-screen">
      <Header />
      <div className="flex flex-1 w-full flex-col lg:flex-row gap-6 px-8 pb-12 max-w-[1400px] mx-auto">
        {/* Repositories panel */}
        <aside className="w-full lg:w-80 shrink-0 flex flex-col gap-4">
          <div className="flex items-center justify-between">
            <h2 className="text-lg font-semibold">Repositories</h2>
            <Button
              onClick={() => {
                loadRepos();
                loadProcessed();
              }}
              className="flex items-center justify-center size-8 rounded-md hover:bg-accent transition"
              aria-label="Refresh repositories"
            >
              <RefreshCw
                className={`size-4 ${
                  reposLoading || processedLoading ? "animate-spin" : ""
                }`}
              />
            </Button>
          </div>

          <div className="flex flex-col gap-2">
            {reposLoading && repos.length === 0 && (
              <div className="text-sm text-muted-foreground">Loading…</div>
            )}
            {reposError && (
              <div className="text-sm text-destructive">{reposError}</div>
            )}
            {!reposLoading && !reposError && repos.length === 0 && (
              <div className="text-sm text-muted-foreground">
                No repositories found. Connect GitHub first.
              </div>
            )}

            {repos.map((repo) => {
              const isSelected = repo === selectedRepo;
              const isProcessing = repo === processingRepo;
              const isProcessed = repo in processedMap;
              const chunkCount = processedMap[repo];
              return (
                <div
                  key={repo}
                  className={`group flex flex-col gap-2 rounded-lg border p-3 transition cursor-pointer ${
                    isSelected
                      ? "border-primary bg-accent"
                      : "border-border hover:bg-accent/50"
                  }`}
                  onClick={() => setSelectedRepo(repo)}
                >
                  <div className="flex items-center gap-2 min-w-0">
                    <FileCode className="size-4 shrink-0 text-muted-foreground" />
                    <span className="text-sm truncate" title={repo}>
                      {repo}
                    </span>
                    {isSelected && (
                      <Check className="size-4 shrink-0 ml-auto text-primary" />
                    )}
                  </div>
                  <div className="flex items-center gap-1.5">
                    {isProcessed ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-primary/15 px-2 py-0.5 text-xs text-primary">
                        <CheckCircle2 className="size-3" />
                        Processed
                        {typeof chunkCount === "number"
                          ? ` · ${chunkCount} chunks`
                          : ""}
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-accent px-2 py-0.5 text-xs text-muted-foreground">
                        <Circle className="size-3" />
                        Not processed
                      </span>
                    )}
                  </div>
                  <Button
                    onClick={(e) => {
                      e.stopPropagation();
                      processRepo(repo);
                    }}
                    disabled={isProcessing}
                    className="flex items-center justify-center gap-2 h-8 rounded-md bg-primary text-primary-foreground text-sm disabled:opacity-60"
                  >
                    {isProcessing ? (
                      <>
                        <Loader2 className="size-4 animate-spin" />
                        Processing…
                      </>
                    ) : isProcessed ? (
                      <>
                        <RefreshCw className="size-3.5" />
                        Reprocess
                      </>
                    ) : (
                      "Process"
                    )}
                  </Button>
                </div>
              );
            })}
          </div>

          {ingestResult && (
            <div className="rounded-lg border border-primary/40 bg-accent/50 p-3 text-sm">
              <div className="font-medium truncate">{ingestResult.repoName}</div>
              <div className="text-muted-foreground">
                {ingestResult.chunks_inserted} chunks ·{" "}
                {ingestResult.embeddings_created} embeddings
              </div>
            </div>
          )}
          {processError && (
            <div className="text-sm text-destructive">{processError}</div>
          )}
        </aside>

        {/* Search panel */}
        <main className="flex-1 flex flex-col gap-4 min-w-0">
          <h2 className="text-lg font-semibold">Ask the codebase</h2>

          <div className="flex flex-col gap-3 rounded-2xl outline-1 outline-accent p-4">
            <Textarea
              placeholder={
                selectedRepo
                  ? `Ask a question about ${selectedRepo}…`
                  : "Select a repository first…"
              }
              className="w-full text-start focus:outline-none field-sizing-content min-h-12 max-h-48 resize-none"
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  e.preventDefault();
                  askAgent();
                }
              }}
            />
            <div className="flex items-center justify-end gap-3">
              {(!selectedRepo || query.trim().length === 0) && (
                <span className="text-xs text-muted-foreground">
                  {!selectedRepo ? "Select a repository" : "Type a question"}
                </span>
              )}
              <Button
                onClick={askAgent}
                disabled={
                  asking || !selectedRepo || query.trim().length === 0
                }
                className="flex items-center justify-center gap-2 h-9 px-4 rounded-md bg-primary text-primary-foreground disabled:opacity-50"
                aria-label="Ask"
              >
                {asking ? (
                  <Loader2 className="size-4 animate-spin" />
                ) : (
                  <ArrowUp className="size-4" />
                )}
                Ask
              </Button>
            </div>
          </div>

          {!selectedRepo && (
            <div className="flex flex-col items-center justify-center gap-2 text-muted-foreground py-16">
              <Search className="size-8" />
              <p className="text-sm">
                Select a repository on the left to start asking questions.
              </p>
            </div>
          )}

          {askError && (
            <div className="text-sm text-destructive">{askError}</div>
          )}

          {answer && (
            <div className="flex flex-col gap-4">
              <div className="rounded-2xl border border-border bg-card p-4">
                <div className="mb-2 text-sm text-muted-foreground">
                  Answer for{" "}
                  <span className="text-foreground">{selectedRepo}</span>
                </div>
                <div className="text-sm leading-relaxed [&_p:not(:last-child)]:mb-3 [&_ul]:mb-3 [&_ul]:list-disc [&_ul]:pl-5 [&_ol]:mb-3 [&_ol]:list-decimal [&_ol]:pl-5 [&_li:not(:last-child)]:mb-1 [&_code]:rounded [&_code]:bg-accent [&_code]:px-1 [&_code]:py-0.5 [&_code]:font-mono [&_code]:text-xs [&_pre]:mb-3 [&_pre]:overflow-x-auto [&_pre]:rounded-lg [&_pre]:border [&_pre]:border-border [&_pre]:bg-muted [&_pre]:p-3 [&_pre_code]:bg-transparent [&_pre_code]:p-0 [&_strong]:font-semibold">
                  <ReactMarkdown>{answer.answer}</ReactMarkdown>
                </div>
              </div>

              {answer.sources.length > 0 && (
                <div className="flex flex-col gap-3">
                  <div className="text-sm text-muted-foreground">
                    {answer.sources.length} source
                    {answer.sources.length === 1 ? "" : "s"}
                  </div>
                  {answer.sources.map((s) => (
                    <SourceCard key={s.chunk_id} source={s} />
                  ))}
                </div>
              )}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function SourceCard({ source }: { source: Source }) {
  return (
    <div className="rounded-xl border border-border p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex flex-col gap-1 min-w-0">
          <div className="flex items-center gap-2 min-w-0">
            <span className="font-mono text-sm truncate">
              {source.symbol_name}
            </span>
            <span className="text-xs rounded-sm bg-accent px-1.5 py-0.5 text-muted-foreground shrink-0">
              {source.symbol_type}
            </span>
            <span className="text-xs rounded-sm bg-accent px-1.5 py-0.5 text-muted-foreground shrink-0">
              {source.language}
            </span>
          </div>
          <div className="text-xs text-muted-foreground truncate">
            {source.file_path}:{source.start_line}-{source.end_line}
          </div>
        </div>
        <div className="text-xs font-mono text-primary shrink-0">
          {source.score.toFixed(3)}
        </div>
      </div>
    </div>
  );
}
