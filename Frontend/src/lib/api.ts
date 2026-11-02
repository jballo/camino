const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

type BackendFetchOptions = {
  method?: "GET" | "POST" | "DELETE";
  body?: unknown;
  signal?: AbortSignal;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
    public retryAfterSeconds?: number,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

/** Human wording for a Retry-After wait, e.g. "in about 2 minutes". */
export function retryWait(seconds: number | undefined) {
  if (seconds === undefined) return "later";
  const minutes = Math.ceil(seconds / 60);
  return minutes <= 1 ? "in about a minute" : `in about ${minutes} minutes`;
}

function retryAfterSeconds(response: Response): number | undefined {
  const value = response.headers.get("Retry-After")?.trim();
  return value && /^\d+$/.test(value) ? Number(value) : undefined;
}

export async function backendFetch<T>(
  path: string,
  token: string,
  options: BackendFetchOptions = {},
): Promise<T> {
  const response = await fetch(`${BACKEND_URL}${path}`, {
    method: options.method ?? "GET",
    headers: {
      Authorization: `Bearer ${token}`,
      ...(options.body === undefined
        ? {}
        : { "Content-Type": "application/json" }),
    },
    body:
      options.body === undefined ? undefined : JSON.stringify(options.body),
    ...(options.signal === undefined ? {} : { signal: options.signal }),
  });

  if (!response.ok) {
    const body: unknown = (await response.json().catch(() => null));

    const message = 
      typeof body === "object" &&
      body !== null && 
      "detail" in body &&
      typeof body.detail === "string"
        ? body.detail 
        : `Request failed (${response.status})`;

    throw new ApiError(
      response.status,
      message,
      retryAfterSeconds(response),
    );

  }

  return (await response.json()) as T;
}
