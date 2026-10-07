import { auth } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

export async function GET(req: NextRequest) {
  const { searchParams } = new URL(req.url);
  const code = searchParams.get("code");
  const installationId = searchParams.get("installation_id");
  const state = searchParams.get("state");

  const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? req.nextUrl.origin;
  const settingsRedirect = (status: string, clearState = true) => {
    const url = new URL("/settings", appUrl);
    url.searchParams.set("github", status);
    const res = NextResponse.redirect(url);
    // Only clear the CSRF state cookie when we actually consumed it as part of
    // an authorization exchange. Clearing it on unrelated requests would break
    // a concurrent in-progress OAuth flow.
    if (clearState) res.cookies.delete("gh_oauth_state");
    return res;
  };

  const { isAuthenticated, getToken } = await auth();

  if (!isAuthenticated) {
    const signInUrl = new URL("/sign-in", appUrl);
    signInUrl.searchParams.set("redirect_url", "/settings");
    return NextResponse.redirect(signInUrl);
  }

  const storedState = req.cookies.get("gh_oauth_state")?.value;
  const hasValidState = Boolean(state && storedState && state === storedState);

  // No OAuth code means this is an installation *update* (repos added/removed),
  // not a fresh authorization. Only show success for callbacks with valid state.
  if (code === null) {
    if (!hasValidState) {
      return NextResponse.redirect(new URL("/settings", appUrl));
    }

    return settingsRedirect("update");
  }

  try {
    if (!hasValidState)
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
