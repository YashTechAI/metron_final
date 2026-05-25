import { NextRequest, NextResponse } from "next/server";

export default function proxy(req: NextRequest) {
  const { pathname } = req.nextUrl;
  const session = req.cookies.get("metron_session")?.value;
  const role = req.cookies.get("metron_role")?.value;

  // /ops is always accessible (super admin login page)
  if (pathname.startsWith("/ops")) return NextResponse.next();

  // /register is removed — no self-signup allowed
  if (pathname.startsWith("/register"))
    return NextResponse.redirect(new URL("/", req.nextUrl));

  // /super — any active session is allowed through; the layout verifies super_admin via API
  if (pathname.startsWith("/super")) {
    if (!session) return NextResponse.redirect(new URL("/ops", req.nextUrl));
    return NextResponse.next();
  }

  // /admin — any active session is allowed through; the layout verifies role via API
  // (cookie role is not trusted here because another browser tab's login can overwrite it)
  if (pathname.startsWith("/admin")) {
    if (!session) return NextResponse.redirect(new URL("/", req.nextUrl));
    return NextResponse.next();
  }

  // /dashboard requires any authenticated session — admins go to their own panel
  if (pathname.startsWith("/dashboard")) {
    if (!session) return NextResponse.redirect(new URL("/", req.nextUrl));
    if (role === "tenant_admin") return NextResponse.redirect(new URL("/admin", req.nextUrl));
    if (role === "super_admin") return NextResponse.redirect(new URL("/super", req.nextUrl));
    return NextResponse.next();
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/super/:path*", "/admin/:path*", "/dashboard/:path*", "/ops/:path*", "/register/:path*", "/register"],
};
