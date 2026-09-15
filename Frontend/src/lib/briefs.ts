import { ApiError, backendFetch } from "./api";
import type { BriefPreview, BriefResponse, BriefSummary } from "../types/brief";

export type TokenGetter = () => Promise<string | null>;

async function tokenOrThrow(getToken: TokenGetter): Promise<string> {
  const token = await getToken();
  if (!token) throw new ApiError(401, "Not authenticated");
  return token;
}

export async function previewIssueBrief(
  issueUrl: string,
  getToken: TokenGetter,
  targetBranch?: string,
): Promise<BriefPreview> {
  return backendFetch<BriefPreview>(
    "/api/v1/briefs/preview",
    await tokenOrThrow(getToken),
    {
      method: "POST",
      body: { issueUrl, ...(targetBranch ? { targetBranch } : {}) },
    },
  );
}

export async function createIssueBrief(
  issueUrl: string,
  targetBranch: string,
  getToken: TokenGetter,
): Promise<{ id: number; status: string }> {
  return backendFetch("/api/v1/briefs", await tokenOrThrow(getToken), {
    method: "POST",
    body: { issueUrl, targetBranch },
  });
}

export async function getIssueBrief(
  id: number | string,
  getToken: TokenGetter,
): Promise<BriefResponse> {
  return backendFetch(
    `/api/v1/briefs/${encodeURIComponent(id)}`,
    await tokenOrThrow(getToken),
  );
}

export async function cancelIssueBrief(
  id: number | string,
  getToken: TokenGetter,
): Promise<BriefResponse> {
  return backendFetch(
    `/api/v1/briefs/${encodeURIComponent(id)}/cancel`,
    await tokenOrThrow(getToken),
    { method: "POST" },
  );
}

export async function listIssueBriefs(
  getToken: TokenGetter,
): Promise<BriefSummary[]> {
  return backendFetch("/api/v1/briefs", await tokenOrThrow(getToken));
}
