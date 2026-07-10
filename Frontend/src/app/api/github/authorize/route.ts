import { auth, currentUser } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const code = searchParams.get("code");
  const installationId = searchParams.get("installation_id");
  const state = searchParams.get("state");

  const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? req.nextUrl.origin;
  const settingsRedirect = (status: string) => {
    const url = new URL("/settings", appUrl);
    url.searchParams.set("github", status);
    const res = NextResponse.redirect(url);
    res.cookies.delete("gh_oauth_state");
    return res;
  };

  const { isAuthenticated, userId, getToken } = await auth();
  const user = await currentUser();

  if (!isAuthenticated || user === null) {
    const signInUrl = new URL("/sign-in", appUrl);
    signInUrl.searchParams.set("redirect_url", "/settings");
    return NextResponse.redirect(signInUrl);
  }

  // No OAuth code means this is an installation *update* (repos added/removed),
  // not a fresh authorization. Nothing to exchange — just return to the app.
  if (code === null) {
    return settingsRedirect("update");
  }

  try {
    const storedState = req.cookies.get("gh_oauth_state")?.value;
    if (!state || !storedState || state !== storedState)
      throw new Error(`Invalid state - possible CSRF`);

    const token = await getToken();
    if (token === null) throw new Error(`Not authenticated`);

    if (installationId === null) throw new Error(`Invalid request`);

    const backend_url = process.env.BACKEND_URL ?? "http://127.0.0.1:8000";

    const response = await fetch(`${backend_url}/api/v1/github/connect`, {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({
        code,
        userId,
        installationId,
      }),
    });

    if (!response.ok) throw new Error("Failed to save github credentials");

    return settingsRedirect("connected");
  } catch (error) {
    console.log("Error: ", error);
    return settingsRedirect("error");
  }
}
