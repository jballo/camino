import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function POST(req: NextRequest) {
  try {
    const { isAuthenticated, userId } = await auth();
    const user = await currentUser();
    const backend_api_key = process.env.BACKEND_API_KEY;

    const body = await req.json();
    const { repoName } = body;
    if (!isAuthenticated || user === null || backend_api_key === undefined)
      throw new Error(`Not authenticated`);

    if (!repoName) throw new Error(`Invalid body`);

    const response = await fetch(
      "http://127.0.0.1:8000/api/repositories/ingest",
      {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          Authorization: `Bearer ${backend_api_key}`,
        },
        body: JSON.stringify({
          repoName,
          userId,
        }),
      },
    );

    if (!response.ok) throw new Error("Failed to process repo");

    const result = await response.json();
    console.log("Result: ", result);
    return new NextResponse(`Success processing ${repoName}`, { status: 200 });
  } catch (error) {
    console.log("Error: ", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
