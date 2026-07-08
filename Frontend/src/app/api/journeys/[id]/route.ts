import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { isAuthenticated, getToken } = await auth();
    const user = await currentUser();

    if (!isAuthenticated || user === null) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getToken();
    if (token === null) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const { id } = await params;

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(
      `${backend_url}/api/v1/journeys/${encodeURIComponent(id)}`,
      {
        method: "GET",
        headers: {
          Authorization: `Bearer ${token}`,
        },
      },
    );

    // Preserve the backend's status (esp. 4xx) instead of collapsing to 500, so
    // the client can react to auth/not-found errors distinctly.
    const result = await response.json().catch(() => null);
    if (!response.ok) {
      return NextResponse.json(
        result ?? { error: "Backend request failed" },
        { status: response.status },
      );
    }
    return NextResponse.json(result);
  } catch (error) {
    console.error("GET /api/journeys/[id] failed:", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
