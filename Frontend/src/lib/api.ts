const BACKEND_URL =
  process.env.NEXT_PUBLIC_BACKEND_URL ?? "http://127.0.0.1:8000";

type BackendFetchOptions = {
  method?: "GET" | "POST";
  body?: unknown;
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = "ApiError";
  }
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
  });

  if (!response.ok) {
    const body = (await response.json().catch(() => null)) as
      | { detail?: string; error?: string }
      | null;

    throw new ApiError(
      response.status,
      body?.detail ?? body?.error ?? `Request failed (${response.status})`,
    );
  }

  return (await response.json()) as T;
}
