import { auth } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";
import crypto from "crypto";

export async function GET() {
  const { isAuthenticated } = await auth();

  if (!isAuthenticated)
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });

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
