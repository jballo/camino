import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import { fetchContributionTarget } from "./contribution-target";

const BACKEND_URL = "http://127.0.0.1:8000";

afterEach(() => {
  vi.unstubAllGlobals();
});

describe("contribution target discovery", () => {
  it("fetches the live target for the selected repository", async () => {
    const target = {
      repoName: "camino/app",
      targetBranch: "develop",
      source: "contributing_doc",
      evidence: "Please target the `develop` branch.",
      evidencePath: "CONTRIBUTING.md",
      defaultBranch: "main",
      checkedAt: "2026-09-13T12:00:00Z",
    };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue(target),
    });
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchContributionTarget(
        "camino/app",
        vi.fn().mockResolvedValue("my-token"),
      ),
    ).resolves.toEqual(target);

    expect(fetchMock).toHaveBeenCalledWith(
      `${BACKEND_URL}/api/v1/repositories/contribution-target?repoName=camino%2Fapp`,
      {
        method: "GET",
        headers: { Authorization: "Bearer my-token" },
        body: undefined,
      },
    );
  });

  it("rejects before fetching when no auth token is available", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);

    await expect(
      fetchContributionTarget("camino/app", vi.fn().mockResolvedValue(null)),
    ).rejects.toEqual(new ApiError(401, "Not authenticated"));
    expect(fetchMock).not.toHaveBeenCalled();
  });
});
