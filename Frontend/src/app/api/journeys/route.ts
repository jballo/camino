import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function POST(req: NextRequest) {
  try {
    const { isAuthenticated, userId, getToken } = await auth();
    const user = await currentUser();

    if (!isAuthenticated || user === null) throw new Error(`Not authenticated`);

    const token = await getToken();
    if (token === null) throw new Error(`Not authenticated`);

    const body = await req.json();
    const { repoName, topic } = body;
    if (!repoName || !topic) throw new Error(`Invalid body`);

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

    if (!response.ok) throw new Error("Failed to create journey");

    const result = await response.json();
    return NextResponse.json(result);
  } catch (error) {
    console.log("Error: ", error);
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

    if (!isAuthenticated || user === null) throw new Error(`Not authenticated`);

    const token = await getToken();
    if (token === null) throw new Error(`Not authenticated`);

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";
    const repo = req.nextUrl.searchParams.get("repo");
    const query = repo ? `?repo=${encodeURIComponent(repo)}` : "";

    const response = await fetch(`${backend_url}/api/v1/journeys${query}`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });

    if (!response.ok) throw new Error("Failed to list journeys");

    const result = await response.json();
    return NextResponse.json(result);
  } catch (error) {
    console.log("Error: ", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
