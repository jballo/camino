import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

export async function GET() {
  const { isAuthenticated, userId, getToken } = await auth();
  const user = await currentUser();

  if (!isAuthenticated || user === null) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  const token = await getToken();
  if (token === null) {
    return NextResponse.json({ error: "Not authenticated" }, { status: 401 });
  }

  const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

  let response: Response;
  try {
    response = await fetch(`${backend_url}/api/v1/github/connection/${userId}`, {
      method: "GET",
      headers: {
        Authorization: `Bearer ${token}`,
      },
    });
  } catch (error) {
    console.log("github/status: backend unreachable at", backend_url, error);
    return NextResponse.json(
      { error: `Backend unreachable at ${backend_url}` },
      { status: 502 },
    );
  }

  if (!response.ok) {
    const detail = await response.text().catch(() => "");
    console.log(
      "github/status: backend responded",
      response.status,
      detail,
    );
    return NextResponse.json(
      { error: `Backend returned ${response.status}` },
      { status: response.status },
    );
  }

  const result = await response.json();
  return NextResponse.json(result);
}
