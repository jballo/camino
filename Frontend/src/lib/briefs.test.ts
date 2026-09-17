import { afterEach, describe, expect, it, vi } from "vitest";

import { ApiError } from "./api";
import {
  createIssueBrief,
  getIssueBrief,
  pollIssueBrief,
  previewIssueBrief,
} from "./briefs";

afterEach(() => vi.unstubAllGlobals());

describe("issue brief client", () => {
  it("previews an issue with an optional branch override", async () => {
    const payload = { title: "Fix it" };
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      json: vi.fn().mockResolvedValue(payload),
    });
    vi.stubGlobal("fetch", fetchMock);
    await expect(
      previewIssueBrief(
        "https://github.com/acme/app/issues/4",
        vi.fn().mockResolvedValue("token"),
        "develop",
      ),
    ).resolves.toEqual(payload);
    expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({
      issueUrl: "https://github.com/acme/app/issues/4",
      targetBranch: "develop",
    });
  });

  it("creates and reads a brief", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ id: 9, status: "pending" }) })
      .mockResolvedValueOnce({ ok: true, status: 200, json: vi.fn().mockResolvedValue({ id: 9, status: "complete" }) });
    vi.stubGlobal("fetch", fetchMock);
    const token = vi.fn().mockResolvedValue("token");
    await createIssueBrief("https://github.com/acme/app/issues/4", "main", token);
    await getIssueBrief(9, token);
    expect(fetchMock.mock.calls[1][0]).toContain("/api/v1/briefs/9");
  });

  it("does not fetch without a token", async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal("fetch", fetchMock);
    await expect(getIssueBrief(1, vi.fn().mockResolvedValue(null))).rejects.toEqual(
      new ApiError(401, "Not authenticated"),
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("polls a brief until it reaches a terminal status", async () => {
    const pending = { id: 9, status: "pending", phase: "queued" };
    const generating = { id: 9, status: "running", phase: "generating" };
    const complete = { id: 9, status: "complete", phase: "complete" };
    const fetchMock = vi
      .fn()
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(pending),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(generating),
      })
      .mockResolvedValueOnce({
        ok: true,
        status: 200,
        json: vi.fn().mockResolvedValue(complete),
      });
    vi.stubGlobal("fetch", fetchMock);
    const getToken = vi.fn().mockResolvedValue("token");
    const onUpdate = vi.fn();

    await expect(
      pollIssueBrief(9, getToken, { intervalMs: 0, onUpdate }),
    ).resolves.toEqual(complete);

    expect(onUpdate.mock.calls.map(([brief]) => brief.status)).toEqual([
      "pending",
      "running",
      "complete",
    ]);
    expect(fetchMock).toHaveBeenCalledTimes(3);
    expect(getToken).toHaveBeenCalledTimes(3);
  });
});
