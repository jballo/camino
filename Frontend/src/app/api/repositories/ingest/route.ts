import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

import { forwardBackendResponse } from "@/lib/backend-response";

export async function POST(req: NextRequest) {
  try {
    const { isAuthenticated, userId, getToken } = await auth();
    const user = await currentUser();

    const body = await req.json();
    const { repoName } = body;
    if (!isAuthenticated || user === null) throw new Error(`Not authenticated`);

    const token = await getToken();
    if (token === null) throw new Error(`Not authenticated`);

    if (!repoName) throw new Error(`Invalid body`);

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(`${backend_url}/api/v1/repositories/ingest`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        repoName,
        userId,
      }),
    });

    if (!response.ok) return forwardBackendResponse(response);

    const result = await response.json();
    console.log("Result: ", result);
    return NextResponse.json({ repoName, ...result });
  } catch (error) {
    console.log("Error: ", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
