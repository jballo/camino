import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError, backendFetch } from "./api";

const BACKEND_URL = "http://127.0.0.1:8000";

function mockResponse(status: number, body: unknown, ok = status < 400) {
  return {
    ok,
    status,
    json:
      body === undefined
        ? vi.fn().mockRejectedValue(new SyntaxError("Unexpected token"))
        : vi.fn().mockResolvedValue(body),
  } as unknown as Response;
}

describe("backendFetch", () => {
  const fetchMock = vi.fn();

  beforeEach(() => {
    vi.stubGlobal("fetch", fetchMock);
  });

  afterEach(() => {
    vi.unstubAllGlobals();
    fetchMock.mockReset();
  });

  it("returns parsed JSON on success", async () => {
    fetchMock.mockResolvedValue(mockResponse(200, { id: 1, name: "camino" }));

    const result = await backendFetch<{ id: number; name: string }>(
      "/journeys",
      "my-token",
    );

    expect(result).toEqual({ id: 1, name: "camino" });
    expect(fetchMock).toHaveBeenCalledWith(`${BACKEND_URL}/journeys`, {
      method: "GET",
      headers: { Authorization: "Bearer my-token" },
      body: undefined,
    });
  });

  it("sends JSON body and Content-Type header on POST", async () => {
    fetchMock.mockResolvedValue(mockResponse(200, {}));

    await backendFetch("/journeys", "my-token", {
      method: "POST",
      body: { title: "New journey" },
    });

    expect(fetchMock).toHaveBeenCalledWith(`${BACKEND_URL}/journeys`, {
      method: "POST",
      headers: {
        Authorization: "Bearer my-token",
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ title: "New journey" }),
    });
  });

  it("throws ApiError with detail message from the backend", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(404, { detail: "Journey not found" }),
    );

    const error = await backendFetch("/journeys/999", "my-token").catch(
      (e: unknown) => e,
    );

    expect(error).toBeInstanceOf(ApiError);
    expect(error).toMatchObject({
      name: "ApiError",
      status: 404,
      message: "Journey not found",
    });
  });

  it("falls back to a generic message when detail is missing", async () => {
    fetchMock.mockResolvedValue(mockResponse(500, { error: "boom" }));

    await expect(backendFetch("/journeys", "my-token")).rejects.toMatchObject({
      status: 500,
      message: "Request failed (500)",
    });
  });

  it("falls back to a generic message when detail is not a string", async () => {
    fetchMock.mockResolvedValue(
      mockResponse(422, { detail: [{ msg: "field required" }] }),
    );

    await expect(backendFetch("/journeys", "my-token")).rejects.toMatchObject({
      status: 422,
      message: "Request failed (422)",
    });
  });

  it("falls back to a generic message when the error body is not JSON", async () => {
    fetchMock.mockResolvedValue(mockResponse(502, undefined, false));

    await expect(backendFetch("/journeys", "my-token")).rejects.toMatchObject({
      status: 502,
      message: "Request failed (502)",
    });
  });

  it("falls back to a generic message when the body is null", async () => {
    fetchMock.mockResolvedValue(mockResponse(400, null, false));

    await expect(backendFetch("/journeys", "my-token")).rejects.toMatchObject({
      status: 400,
      message: "Request failed (400)",
    });
  });
});
