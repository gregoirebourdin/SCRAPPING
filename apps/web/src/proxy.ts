import { getSessionCookie } from "better-auth/cookies";
import { type NextRequest, NextResponse } from "next/server";

/** Fast path gate (cookie presence). Route handlers re-validate the session authoritatively. */
export function proxy(request: NextRequest) {
  const cookie = getSessionCookie(request, { cookiePrefix: "scout" });
  const { pathname } = request.nextUrl;
  const isAuthPage = pathname.startsWith("/sign-in") || pathname.startsWith("/sign-up");
  if (!cookie && !isAuthPage) {
    const url = new URL("/sign-in", request.url);
    if (pathname !== "/") url.searchParams.set("next", pathname);
    return NextResponse.redirect(url);
  }
  if (cookie && isAuthPage) {
    return NextResponse.redirect(new URL("/", request.url));
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!api|_next/static|_next/image|favicon.ico|icon.svg|brand/|robots.txt).*)"],
};
