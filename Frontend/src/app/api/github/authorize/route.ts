import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(req: NextRequest) {
  try {
    const { isAuthenticated, userId } = await auth();
    const user = await currentUser();
    const backend_api_key = process.env.BACKEND_API_KEY;
    const { searchParams } = new URL(req.url);
    const code = searchParams.get("code");
    const installationId = searchParams.get("installation_id");
    const state = searchParams.get("state");
    const storedState = req.cookies.get("gh_oauth_state")?.value;

    if (!state || !storedState || state !== storedState)
      throw new Error(`Invalid state - possible CSRF`);

    if (!isAuthenticated || user === null || backend_api_key === undefined)
      throw new Error(`Not authenticated`);

    if (code === null || installationId === null)
      throw new Error(`Invalid request`);

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(`${backend_url}/api/v1/github/connect`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${backend_api_key}`,
      },
      body: JSON.stringify({
        code,
        userId,
        installationId,
      }),
    });

    if (!response.ok) throw new Error("Failed to save github credentials");

    const result = await response.text();
    console.log("result: ", result);
    const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? "http://localhost:3000";
    const redirectResponse = NextResponse.redirect(new URL("/", appUrl));
    redirectResponse.cookies.delete("gh_oauth_state");
    return redirectResponse;
  } catch (error) {
    console.log("Error: ", error);
    return NextResponse.json(
      { error: "Internal Server Error" },
      { status: 500 },
    );
  }
}
