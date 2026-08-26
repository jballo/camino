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
    );

  }

  return (await response.json()) as T;
}
