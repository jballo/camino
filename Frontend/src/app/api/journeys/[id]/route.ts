import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(
  _req: NextRequest,
  { params }: { params: Promise<{ id: string }> },
) {
  try {
    const { isAuthenticated, getToken } = await auth();
    const user = await currentUser();

    if (!isAuthenticated || user === null) throw new Error(`Not authenticated`);

    const token = await getToken();
    if (token === null) throw new Error(`Not authenticated`);

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

    if (!response.ok) throw new Error("Failed to get journey");

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
