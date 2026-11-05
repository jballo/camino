import type { BriefSummary } from "../types/brief";

/** The briefs the rail shows for a filter query. */
export function filterBriefs(
  briefs: BriefSummary[],
  query: string,
): BriefSummary[] {
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
}

/**
 * The brief to select after `deletedId` is deleted: the next visible one,
 * else the previous visible one, else nothing. If the deleted brief was
 * hidden by the filter, the first visible brief.
 *
 * `visible` can lag behind deletions that finished since the last render,
 * so briefs in `deleted` are never picked; `deletedId` itself stays in the
 * list only to mark the position.
 */
export function nextSelection(
  visible: BriefSummary[],
  deletedId: number,
  deleted: ReadonlySet<number> = new Set(),
): number | undefined {
  const candidates = visible.filter(
    (brief) => brief.id === deletedId || !deleted.has(brief.id),
  );
  const index = candidates.findIndex((brief) => brief.id === deletedId);
  if (index === -1) return candidates[0]?.id;
  return (candidates[index + 1] ?? candidates[index - 1])?.id;
}

/**
 * The selection after the brief list reloads. A current selection is kept.
 * With none, the newest brief is selected, unless the selection was cleared
 * on purpose (deleting the last brief the filter showed).
 */
export function resolveSelection(
  current: number | undefined,
  briefs: BriefSummary[],
  cleared: boolean,
): number | undefined {
  if (current !== undefined) return current;
  return cleared ? undefined : briefs[0]?.id;
}
