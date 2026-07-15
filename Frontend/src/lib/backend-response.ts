import { NextResponse } from "next/server";


export async function forwardBackendResponse(response: Response) {
  const body = await response.json().catch(() => null);
  const retryAfter = response.headers.get("retry-after");
  const headers = retryAfter ? { "Retry-After": retryAfter } : undefined;

  if (body === null && response.ok) {
    return new NextResponse(null, { status: response.status, headers });
  }

  return NextResponse.json(
    body ?? { error: "Backend request failed" },
    { status: response.status, headers },
  );
}
