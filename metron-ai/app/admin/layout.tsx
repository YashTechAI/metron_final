"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { fetchAuthSession, signOut } from "aws-amplify/auth";

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const [email, setEmail] = useState("");
  const [tenantName, setTenantName] = useState("");
  const [ready, setReady] = useState(false);

  useEffect(() => {
    fetchAuthSession().then(async session => {
      if (!session.tokens?.idToken) { window.location.href = "/"; return; }
      const token = session.tokens.idToken.toString();
      const sessionEmail = session.tokens.idToken.payload?.email as string || "";
      // If another tab logged in as a different user, Amplify's localStorage is theirs — go back to login
      const cachedEmail = sessionStorage.getItem("metron_user_email");
      if (cachedEmail && sessionEmail && cachedEmail !== sessionEmail) {
        window.location.href = "/";
        return;
      }
      const r = await fetch("/api/quota", { headers: { Authorization: `Bearer ${token}` } });
      if (!r.ok) { window.location.href = "/"; return; }
      const d = await r.json();
      if (!["tenant_admin", "super_admin"].includes(d.role)) { window.location.href = "/"; return; }
      setEmail(d.email || sessionEmail);
      setTenantName(d.tenant_name || "");
      setReady(true);
    }).catch(() => { window.location.href = "/"; });
  }, []);

  const handleLogout = async () => {
    await signOut();
    document.cookie = "metron_session=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
    document.cookie = "metron_role=; expires=Thu, 01 Jan 1970 00:00:00 GMT; path=/";
    window.location.href = "/";
  };

  const navLinks = [
    { label: "Team", icon: "group", href: "/admin" },
    { label: "All Runs", icon: "history", href: "/admin/runs" },
  ];

  if (!ready) {
    return (
      <div className="min-h-screen bg-[var(--color-background)] flex items-center justify-center">
        <span className="material-symbols-outlined animate-spin text-primary text-4xl">progress_activity</span>
      </div>
    );
  }

  return (
    <div className="flex h-screen bg-[var(--color-background)] overflow-hidden">
      {/* Sidebar */}
      <aside className="w-64 flex flex-col bg-[var(--color-surface-container-lowest)] border-r border-[var(--color-outline-variant)]/30">
        {/* Logo */}
        <div className="p-6 border-b border-[var(--color-outline-variant)]/20">
          <div className="flex items-center gap-3">
            <div className="w-9 h-9 rounded-xl bg-[var(--color-primary)] flex items-center justify-center shadow-lg shadow-[#00668a]/20">
              <span className="material-symbols-outlined text-white text-xl">manage_accounts</span>
            </div>
            <div>
              <p className="font-headline font-black text-sm text-[var(--color-on-surface)] tracking-tight">Admin Panel</p>
              <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60 truncate max-w-[120px]">
                {tenantName || "Team Management"}
              </p>
            </div>
          </div>
        </div>

        {/* Nav */}
        <nav className="flex-1 p-4 space-y-1">
          {navLinks.map(link => {
            const isActive = pathname === link.href;
            return (
              <a key={link.href} href={link.href}
                className={`flex items-center gap-3 px-4 py-3 rounded-xl text-sm font-bold transition-all ${
                  isActive
                    ? "bg-[var(--color-primary)] text-white shadow-lg shadow-[#00668a]/20"
                    : "text-[var(--color-on-surface-variant)] hover:bg-[var(--color-surface-container-low)]"
                }`}>
                <span className="material-symbols-outlined text-xl">{link.icon}</span>
                {link.label}
              </a>
            );
          })}
        </nav>

        {/* User */}
        <div className="p-4 border-t border-[var(--color-outline-variant)]/10">
          <div className="flex items-center gap-3 p-2.5 rounded-2xl bg-[var(--color-surface-container-low)]">
            <div className="w-9 h-9 rounded-full bg-gradient-to-br from-[#00668a] to-[#38bdf8] flex items-center justify-center text-white text-xs font-black flex-shrink-0">
              {email ? email[0].toUpperCase() : "A"}
            </div>
            <div className="flex-1 min-w-0">
              <p className="text-xs font-black text-[var(--color-on-surface)] truncate">{email}</p>
              <p className="text-[10px] text-[var(--color-primary)] font-bold">Team Admin</p>
            </div>
            <button onClick={handleLogout} className="text-[var(--color-outline)] hover:text-red-500 hover:bg-red-50 rounded-lg p-1 transition-all">
              <span className="material-symbols-outlined text-xl">logout</span>
            </button>
          </div>
        </div>
      </aside>

      {/* Main */}
      <main className="flex-1 flex flex-col overflow-hidden">
        <header className="h-14 flex items-center px-8 bg-white border-b border-[var(--color-outline-variant)]/20">
          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-[var(--color-primary)]" />
            <span className="text-[10px] font-black uppercase tracking-[0.2em] text-[var(--color-primary)]">Team Admin</span>
            <span className="text-[var(--color-outline-variant)] mx-2">·</span>
            <span className="text-[10px] text-[var(--color-on-surface-variant)] uppercase tracking-widest opacity-60">
              {tenantName}
            </span>
          </div>
        </header>
        <div className="flex-1 overflow-y-auto p-8 bg-[var(--color-background)]">
          {children}
        </div>
      </main>
    </div>
  );
}
