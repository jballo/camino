import { ApiError, backendFetch } from "./api";
import type { ContributionTarget } from "../types/contribution-target";

export async function fetchContributionTarget(
  repoName: string,
  getToken: () => Promise<string | null>,
  signal?: AbortSignal,
): Promise<ContributionTarget> {
  const token = await getToken();
  if (!token) throw new ApiError(401, "Not authenticated");

  return backendFetch<ContributionTarget>(
    `/api/v1/repositories/contribution-target?repoName=${encodeURIComponent(repoName)}`,
    token,
    { signal },
  );
}
