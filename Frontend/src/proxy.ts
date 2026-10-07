import { clerkMiddleware, createRouteMatcher } from "@clerk/nextjs/server";
import { NextResponse } from "next/server";

const isWorkbenchRoute = createRouteMatcher(["/briefs(.*)"]);

export default clerkMiddleware(async (auth, req) => {
  const { pathname, search } = req.nextUrl;

  // The landing page is for visitors; signed-in users go straight to work.
  if (pathname === "/") {
    const { isAuthenticated } = await auth();
    if (isAuthenticated) return NextResponse.redirect(new URL("/briefs", req.url));
    return;
  }

  // Briefs need a session; come back to the same page after signing in.
  if (isWorkbenchRoute(req)) {
    const { isAuthenticated } = await auth();
    if (!isAuthenticated) {
      const signIn = new URL("/sign-in", req.url);
      signIn.searchParams.set("redirect_url", `${pathname}${search}`);
      return NextResponse.redirect(signIn);
    }
  }
});

export const config = {
  matcher: [
    // Skip Next.js internals and all static files, unless found in search params
    "/((?!_next|[^?]*\\.(?:html?|css|js(?!on)|jpe?g|webp|png|gif|svg|ttf|woff2?|ico|csv|docx?|xlsx?|zip|webmanifest)).*)",
    // Always run for API routes
    "/(api|trpc)(.*)",
  ],
};
