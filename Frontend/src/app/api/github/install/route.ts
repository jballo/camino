import { auth } from "@clerk/nextjs/server";
import { NextResponse, type NextRequest } from "next/server";
import crypto from "crypto";

export async function GET(req: NextRequest) {
  const { isAuthenticated } = await auth();

  if (!isAuthenticated) {
    const appUrl = process.env.NEXT_PUBLIC_APP_URL ?? req.nextUrl.origin;
    const signInUrl = new URL("/sign-in", appUrl);
    signInUrl.searchParams.set("redirect_url", "/settings");
    return NextResponse.redirect(signInUrl);
  }

  const state = crypto.randomBytes(32).toString("hex");
  const ghUrl = `https://github.com/apps/camino-onboarder/installations/new?state=${state}`;

  const response = NextResponse.redirect(ghUrl);
  response.cookies.set("gh_oauth_state", state, {
    httpOnly: true,
    secure: process.env.NODE_ENV === "production",
    sameSite: "lax",
    maxAge: 600,
    path: "/",
  });
  return response;
}
