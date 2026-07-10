import { auth } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";

// GitHub App "Setup URL" landing. Hit after a user installs or updates
// (adds/removes repos on) the installation, when "Redirect on update" is
// enabled. Unlike /authorize, there is no OAuth `code` on updates — the
// connection already exists, so we just bounce the user back into the app.
export async function GET(req: NextRequest) {
  const { isAuthenticated } = await auth();
  const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? req.nextUrl.origin;

  if (!isAuthenticated) {
    const signInUrl = new URL("/sign-in", appUrl);
    signInUrl.searchParams.set("redirect_url", "/settings");
    return NextResponse.redirect(signInUrl);
  }

  const setupAction = req.nextUrl.searchParams.get("setup_action");
  const settingsUrl = new URL("/settings", appUrl);
  if (setupAction) settingsUrl.searchParams.set("github", setupAction);
  return NextResponse.redirect(settingsUrl);
}
