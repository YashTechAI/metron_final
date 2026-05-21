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

  // /super requires super_admin role
  if (pathname.startsWith("/super")) {
    if (!session || role !== "super_admin")
      return NextResponse.redirect(new URL("/ops", req.nextUrl));
    return NextResponse.next();
  }

  // /admin requires tenant_admin or super_admin
  if (pathname.startsWith("/admin")) {
    if (!session) return NextResponse.redirect(new URL("/", req.nextUrl));
    if (!["tenant_admin", "super_admin"].includes(role ?? ""))
      return NextResponse.redirect(new URL("/dashboard", req.nextUrl));
    return NextResponse.next();
  }

  // /dashboard requires any authenticated session
  if (pathname.startsWith("/dashboard")) {
    if (!session) return NextResponse.redirect(new URL("/", req.nextUrl));
    return NextResponse.next();
  }

  return NextResponse.next();
}

export const config = {
  matcher: ["/super/:path*", "/admin/:path*", "/dashboard/:path*", "/ops/:path*", "/register/:path*", "/register"],
};
