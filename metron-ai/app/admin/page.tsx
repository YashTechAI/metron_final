"use client";

import { useState, useEffect, useCallback } from "react";
import { authFetch } from "@/lib/api";

interface TenantUser {
  user_email: string;
  role: string;
  run_limit: number;
  runs_used: number;
}

interface TenantStats {
  tenant_id: string;
  name: string;
  quota_limit: number;
  quota_used: number;
  users: TenantUser[];
  total_runs_all_time: number;
}

const ROLES = ["viewer", "functional_tester", "security_tester", "all"];

function roleColor(role: string) {
  return role === "tenant_admin"
    ? "bg-primary/10 text-primary"
    : role === "security_tester"
    ? "bg-error/10 text-error"
    : role === "functional_tester"
    ? "bg-secondary/10 text-secondary"
    : "bg-[var(--color-surface-container)]/60 text-[var(--color-on-surface-variant)]";
}

export default function AdminPage() {
  const [stats, setStats] = useState<TenantStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [showAdd, setShowAdd] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState("viewer");
  const [newLimit, setNewLimit] = useState(10);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const r = await authFetch("/api/admin/stats");
      if (r.status === 403) { setError("You do not have admin access."); return; }
      if (!r.ok) { setError("Failed to load admin data."); return; }
      setStats(await r.json());
    } catch { setError("Network error."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const addUser = async () => {
    if (!newEmail.trim()) return;
    setSaving(true); setSaveMsg("");
    const r = await authFetch("/api/admin/users", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ user_email: newEmail.trim(), role: newRole, run_limit: newLimit }),
    });
    setSaving(false);
    if (r.ok) {
      const d = await r.json();
      setSaveMsg(d.invite_sent
        ? `Invite sent to ${newEmail.trim()}. They'll receive an email with login instructions.`
        : `User added to team (invite email could not be sent — check Cognito settings).`);
      setNewEmail(""); setNewRole("viewer"); setNewLimit(10);
      setShowAdd(false);
      load();
    } else {
      const d = await r.json().catch(() => ({}));
      setSaveMsg(d.detail || "Failed to add user.");
    }
  };

  const updateUser = async (email: string, field: "role" | "run_limit", value: string | number) => {
    await authFetch(`/api/admin/users/${encodeURIComponent(email)}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ [field]: value }),
    });
    load();
  };

  const removeUser = async (email: string) => {
    await authFetch(`/api/admin/users/${encodeURIComponent(email)}`, { method: "DELETE" });
    load();
  };

  if (loading) return (
    <div className="flex items-center justify-center py-20">
      <span className="material-symbols-outlined text-4xl animate-spin text-primary">progress_activity</span>
    </div>
  );

  if (error) return (
    <div className="card p-6 border-error/30 bg-error/5 flex items-center gap-3">
      <span className="material-symbols-outlined text-error">error</span>
      <p className="text-sm text-error">{error}</p>
    </div>
  );

  if (!stats) return null;

  const quotaPct = stats.quota_limit > 0 ? Math.round((stats.quota_used / stats.quota_limit) * 100) : 0;

  return (
    <div className="max-w-4xl mx-auto space-y-8 animate-fade-in">
      {/* Header */}
      <div className="space-y-1">
        <h1 className="font-headline text-4xl font-black tracking-tighter">Team Management</h1>
        <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">
          Manage access and run limits for <span className="font-bold text-[var(--color-on-surface)]">{stats.name}</span>
        </p>
      </div>

      {/* Stats */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: "Team Size", value: stats.users.length, icon: "group" },
          { label: "Quota Used", value: `${stats.quota_used}/${stats.quota_limit > 0 ? stats.quota_limit : "∞"}`, icon: "data_usage" },
          { label: "All-time Runs", value: stats.total_runs_all_time, icon: "history" },
          { label: "Quota %", value: `${quotaPct}%`, icon: "percent" },
        ].map(({ label, value, icon }) => (
          <div key={label} className="card p-4 space-y-1">
            <div className="flex items-center gap-2 text-[var(--color-on-surface-variant)]">
              <span className="material-symbols-outlined text-base">{icon}</span>
              <span className="text-[10px] font-black uppercase tracking-wider opacity-60">{label}</span>
            </div>
            <p className="font-headline text-2xl font-black">{value}</p>
          </div>
        ))}
      </div>

      {/* Quota bar */}
      {stats.quota_limit > 0 && (
        <div className="card p-5 space-y-2">
          <div className="flex items-center justify-between text-xs">
            <span className="font-bold">Organisation quota this period</span>
            <span className={`font-black ${quotaPct >= 100 ? "text-error" : quotaPct >= 80 ? "text-[#855300]" : "text-primary"}`}>{quotaPct}%</span>
          </div>
          <div className="h-2 rounded-full bg-[var(--color-outline-variant)]/30 overflow-hidden">
            <div className={`h-full rounded-full transition-all ${quotaPct >= 100 ? "bg-error" : quotaPct >= 80 ? "bg-[#855300]" : "bg-primary"}`}
              style={{ width: `${Math.min(quotaPct, 100)}%` }} />
          </div>
          <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">
            {stats.quota_used} of {stats.quota_limit} runs used · resets monthly
          </p>
        </div>
      )}

      {/* Success/error message */}
      {saveMsg && (
        <div className={`p-4 rounded-xl text-sm font-medium ${saveMsg.includes("sent") || saveMsg.includes("added") ? "bg-secondary/10 text-secondary" : "bg-error/10 text-error"}`}>
          {saveMsg}
        </div>
      )}

      {/* Team table */}
      <div className="card overflow-hidden">
        <div className="flex items-center justify-between p-5 border-b border-[var(--color-outline-variant)]/20">
          <h2 className="font-headline font-black text-lg">Team Members</h2>
          <button onClick={() => { setShowAdd(!showAdd); setSaveMsg(""); }}
            className="flex items-center gap-2 px-4 py-2 rounded-xl btn-primary text-xs font-bold">
            <span className="material-symbols-outlined text-sm">person_add</span>
            Invite Member
          </button>
        </div>

        {showAdd && (
          <div className="p-5 border-b border-[var(--color-outline-variant)]/20 bg-[var(--color-surface-container-low)] space-y-3">
            <p className="text-xs font-black uppercase tracking-wider text-primary">Invite New Member</p>
            <p className="text-[11px] text-[var(--color-on-surface-variant)] opacity-60">
              They'll receive an email with a temporary password to set up their account.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-3 gap-3">
              <input value={newEmail} onChange={e => setNewEmail(e.target.value)}
                placeholder="user@company.com" type="email"
                className="px-3 py-2.5 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white" />
              <select value={newRole} onChange={e => setNewRole(e.target.value)}
                className="px-3 py-2.5 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white">
                {ROLES.map(r => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
              </select>
              <div className="flex items-center gap-2">
                <input value={newLimit} onChange={e => setNewLimit(Number(e.target.value))}
                  type="number" min={0} placeholder="Run limit"
                  className="flex-1 px-3 py-2.5 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white" />
                <button onClick={addUser} disabled={saving}
                  className="px-4 py-2.5 rounded-xl btn-primary text-xs font-bold whitespace-nowrap disabled:opacity-50">
                  {saving ? "Inviting…" : "Send Invite"}
                </button>
              </div>
            </div>
          </div>
        )}

        <div className="divide-y divide-[var(--color-outline-variant)]/10">
          {stats.users.filter(u => u.role !== "tenant_admin").length === 0 ? (
            <div className="p-12 text-center space-y-2">
              <span className="material-symbols-outlined text-3xl text-[var(--color-outline)] opacity-40">group_add</span>
              <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">No team members yet. Invite your first member above.</p>
            </div>
          ) : stats.users.filter(u => u.role !== "tenant_admin").map(u => {
            const userPct = u.run_limit > 0 ? Math.round((u.runs_used / u.run_limit) * 100) : 0;
            return (
              <div key={u.user_email} className="p-4 flex items-center gap-4 flex-wrap">
                <div className="flex items-center gap-3 flex-1 min-w-0">
                  <div className="w-9 h-9 rounded-full bg-gradient-to-br from-[#00668a] to-[#38bdf8] flex items-center justify-center text-white text-xs font-black flex-shrink-0">
                    {u.user_email[0].toUpperCase()}
                  </div>
                  <div className="min-w-0">
                    <p className="text-sm font-bold truncate">{u.user_email}</p>
                    <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">
                      {u.runs_used}/{u.run_limit > 0 ? u.run_limit : "∞"} runs used
                      {u.run_limit > 0 && ` (${userPct}%)`}
                    </p>
                  </div>
                </div>
                <select defaultValue={u.role}
                  onChange={e => updateUser(u.user_email, "role", e.target.value)}
                  className={`text-[11px] font-bold px-2 py-1 rounded-full border-0 cursor-pointer ${roleColor(u.role)}`}>
                  {ROLES.map(r => <option key={r} value={r}>{r.replace(/_/g, " ")}</option>)}
                </select>
                <div className="flex items-center gap-1.5">
                  <span className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">Limit:</span>
                  <input defaultValue={u.run_limit} type="number" min={0}
                    onBlur={e => updateUser(u.user_email, "run_limit", Number(e.target.value))}
                    className="w-16 text-xs text-center px-2 py-1 rounded-lg border border-[var(--color-outline-variant)] bg-white" />
                </div>
                <button onClick={() => removeUser(u.user_email)}
                  className="w-8 h-8 rounded-lg flex items-center justify-center text-[var(--color-outline)] hover:text-error hover:bg-error/10 transition-all">
                  <span className="material-symbols-outlined text-base">person_remove</span>
                </button>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
