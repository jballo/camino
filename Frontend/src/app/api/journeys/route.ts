import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

import { forwardBackendResponse } from "@/lib/backend-response";

export async function POST(req: NextRequest) {
  try {
    const { isAuthenticated, userId, getToken } = await auth();
    const user = await currentUser();

    if (!isAuthenticated || user === null) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const token = await getToken();
    if (token === null) {
      return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
    }

    const body = await req.json().catch(() => null);
    const { repoName, topic } = body ?? {};
    if (!repoName || !topic) {
      return NextResponse.json(
        { error: "repoName and topic are required" },
        { status: 400 },
      );
    }

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(`${backend_url}/api/v1/journeys`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        repoName,
        topic,
        userId,
      }),
    });

    return forwardBackendResponse(response);
  } catch (error) {
    console.error("POST /api/journeys failed:", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}

export async function GET(req: NextRequest) {
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

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
    const repo = req.nextUrl.searchParams.get("repo");
    const query = repo ? `?repo=${encodeURIComponent(repo)}` : "";

    const response = await fetch(`${backend_url}/api/v1/journeys${query}`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    return forwardBackendResponse(response);
  } catch (error) {
    console.error("GET /api/journeys failed:", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
