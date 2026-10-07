"use client";

import { Search } from "lucide-react";
import { useMemo, useState } from "react";

import type { BriefSummary } from "@/types/brief";
import type { JourneyStatus } from "@/types/tour";

type BriefRailProps = {
  briefs: BriefSummary[];
  selectedId: number | undefined;
  onSelect: (id: number) => void;
};

const STATUS: Record<
  JourneyStatus,
  { label: string; dotClassName: string; textClassName: string }
> = {
  complete: {
    label: "Ready",
    dotClassName: "bg-success",
    textClassName: "text-muted-foreground",
  },
  generating: {
    label: "Generating",
    dotClassName: "status-dot",
    textClassName: "text-brand-accent",
  },
  running: {
    label: "Generating",
    dotClassName: "status-dot",
    textClassName: "text-brand-accent",
  },
  pending: {
    label: "Queued",
    dotClassName: "bg-muted-foreground",
    textClassName: "text-muted-foreground",
  },
  failed: {
    label: "Failed",
    dotClassName: "bg-destructive",
    textClassName: "text-destructive",
  },
  cancelled: {
    label: "Cancelled",
    dotClassName: "bg-muted-foreground",
    textClassName: "text-muted-foreground",
  },
};

export default function BriefRail({
  briefs,
  selectedId,
  onSelect,
}: BriefRailProps) {
  const [query, setQuery] = useState("");
  const filteredBriefs = useMemo(() => {
    const normalized = query.trim().toLowerCase();
    if (!normalized) return briefs;

    return briefs.filter((brief) =>
      [
        brief.issueTitle,
        brief.issueRepo,
        `#${brief.issueNumber}`,
        `${brief.issueRepo} #${brief.issueNumber}`,
      ].some((value) => value.toLowerCase().includes(normalized)),
    );
  }, [briefs, query]);

  return (
    <aside className="console" aria-label="Brief list">
      <div className="flex items-center gap-2.5 border-b border-border px-4 py-3">
        <Search aria-hidden="true" className="size-3.5 shrink-0 text-muted-foreground" />
        <label htmlFor="brief-filter" className="sr-only">
          Filter briefs
        </label>
        <input
          id="brief-filter"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="Filter briefs…"
          className="min-h-8 min-w-0 flex-1 bg-transparent font-mono text-xs text-foreground outline-none placeholder:text-muted-foreground"
        />
      </div>

      <div>
        {filteredBriefs.map((brief) => {
          const selected = brief.id === selectedId;
          const status = STATUS[brief.status];
          return (
            <button
              key={brief.id}
              type="button"
              onClick={() => onSelect(brief.id)}
              aria-current={selected ? "true" : undefined}
              className={`flex w-full flex-col gap-1 border-b border-l-[3px] border-b-border px-4 py-3.5 text-left transition last:border-b-0 hover:bg-accent ${
                selected
                  ? "border-l-brand-accent bg-accent"
                  : "border-l-transparent"
              }`}
            >
              <span className="line-clamp-2 text-sm font-medium leading-snug">
                {brief.issueTitle}
              </span>
              <span className="flex w-full flex-wrap items-center gap-2 font-mono text-[10px] text-muted-foreground">
                <span
                  aria-hidden="true"
                  className={`size-2 shrink-0 rounded-full ${status.dotClassName}`}
                />
                <span className={`uppercase tracking-[.08em] ${status.textClassName}`}>
                  {status.label}
                </span>
                <span aria-hidden="true">·</span>
                <span className="break-all">
                  {brief.issueRepo} #{brief.issueNumber}
                </span>
                {brief.id === briefs[0]?.id && (
                  <span className="ml-auto rounded-full bg-primary/10 px-2 py-0.5 text-[9px] uppercase tracking-[.12em] text-brand-accent">
                    Latest
                  </span>
                )}
              </span>
            </button>
          );
        })}

        {filteredBriefs.length === 0 && (
          <p className="px-4 py-8 text-center text-sm text-muted-foreground">
            No briefs match “{query.trim()}”.
          </p>
        )}
      </div>
    </aside>
  );
}
