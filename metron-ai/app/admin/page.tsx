"use client";

import { useState, useEffect, useCallback } from "react";
import { authFetch } from "@/lib/api";
import { ProgressFill } from "@/components/ProgressFill";

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
  admin_email: string;
}

// ── Phase helpers ─────────────────────────────────────────────────────────────
const PHASES = [
  { key: "functional",  label: "Functional",  color: "bg-secondary/10 text-secondary" },
  { key: "security",    label: "Security",    color: "bg-error/10 text-error" },
  { key: "quality",     label: "Quality",     color: "bg-primary/10 text-primary" },
  { key: "performance", label: "Performance", color: "bg-[#855300]/10 text-[#855300]" },
  { key: "load",        label: "Load",        color: "bg-[var(--color-surface-container)] text-[var(--color-on-surface-variant)]" },
];
const KNOWN_PHASES = new Set(PHASES.map(p => p.key));

function parseRole(role: string): Set<string> {
  const legacy: Record<string, string[]> = {
    "functional_tester":   ["functional"],
    "security_tester":     ["security"],
    "quality":             ["quality"],
    "performance":         ["performance"],
    "load":                ["load"],
    "security+functional": ["security", "functional"],
    "functional+quality":  ["functional", "quality"],
    "performance+load":    ["performance", "load"],
    "all":                 ["functional", "security", "quality", "performance", "load"],
    "tenant_admin":        ["functional", "security", "quality", "performance", "load"],
    "super_admin":         ["functional", "security", "quality", "performance", "load"],
  };
  if (legacy[role]) return new Set(legacy[role]);
  const parts = role.split("+").filter(p => KNOWN_PHASES.has(p));
  return parts.length > 0 ? new Set(parts) : new Set(KNOWN_PHASES);
}

function buildRole(phases: Set<string>): string {
  const order = ["functional", "security", "quality", "performance", "load"];
  const active = order.filter(p => phases.has(p));
  if (active.length === order.length) return "all";
  return active.join("+");
}

function roleLabel(role: string): string {
  const phases = parseRole(role);
  if (phases.size === PHASES.length) return "All Tests";
  return PHASES.filter(p => phases.has(p.key)).map(p => p.label).join(" + ");
}

// ── Phase checkbox picker component ──────────────────────────────────────────
function PhasePicker({ value, onChange }: { value: string; onChange: (role: string) => void }) {
  const checked = parseRole(value);
  const toggle = (key: string) => {
    const next = new Set(checked);
    next.has(key) ? next.delete(key) : next.add(key);
    if (next.size === 0) return; // must keep at least one
    onChange(buildRole(next));
  };
  return (
    <div className="flex flex-wrap gap-2">
      {PHASES.map(p => (
        <button
          key={p.key}
          type="button"
          onClick={() => toggle(p.key)}
          className={`flex items-center gap-1.5 px-3 py-1.5 rounded-xl text-xs font-bold border-2 transition-all ${
            checked.has(p.key)
              ? `${p.color} border-current`
              : "bg-[var(--color-surface-container-low)] text-[var(--color-on-surface-variant)] opacity-40 border-transparent hover:opacity-70"
          }`}
        >
          <span className={`w-3.5 h-3.5 rounded flex items-center justify-center border ${
            checked.has(p.key) ? "bg-current border-current" : "border-[var(--color-outline)]"
          }`}>
            {checked.has(p.key) && (
              <svg viewBox="0 0 10 8" className="w-2.5 h-2 fill-white"><path d="M1 4l3 3 5-6"/></svg>
            )}
          </span>
          {p.label}
        </button>
      ))}
    </div>
  );
}

function roleColor(role: string) {
  const phases = parseRole(role);
  if (phases.has("security") && phases.size === 1) return "bg-error/10 text-error";
  if (phases.has("functional") && phases.size === 1) return "bg-secondary/10 text-secondary";
  if (phases.size === PHASES.length) return "bg-primary/10 text-primary";
  return "bg-[var(--color-surface-container)] text-[var(--color-on-surface-variant)]";
}

export default function AdminPage() {
  const [stats, setStats] = useState<TenantStats | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const [showAdd, setShowAdd] = useState(false);
  const [newEmail, setNewEmail] = useState("");
  const [newRole, setNewRole] = useState("functional+security");
  const [newLimit, setNewLimit] = useState(10);
  const [expandedUser, setExpandedUser] = useState<string | null>(null);
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

  const teamMembers = (stats.users ?? []).filter(u => u.user_email !== stats.admin_email && u.role !== "tenant_admin");
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
          { label: "Team Size", value: teamMembers.length, icon: "group" },
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
            <ProgressFill pct={quotaPct} className={`h-full rounded-full transition-all ${quotaPct >= 100 ? "bg-error" : quotaPct >= 80 ? "bg-[#855300]" : "bg-primary"}`} />
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
          <div className="p-5 border-b border-[var(--color-outline-variant)]/20 bg-[var(--color-surface-container-low)] space-y-4">
            <p className="text-xs font-black uppercase tracking-wider text-primary">Invite New Member</p>
            <p className="text-[11px] text-[var(--color-on-surface-variant)] opacity-60">
              They&apos;ll receive an email with a temporary password to set up their account.
            </p>
            <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
              <input value={newEmail} onChange={e => setNewEmail(e.target.value)}
                placeholder="user@company.com" type="email"
                className="px-3 py-2.5 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white" />
              <div className="flex items-center gap-2">
                <input value={newLimit} onChange={e => setNewLimit(Number(e.target.value))}
                  type="number" min={0} placeholder="Run limit"
                  className="w-32 px-3 py-2.5 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white" />
                <button onClick={addUser} disabled={saving}
                  className="flex-1 px-4 py-2.5 rounded-xl btn-primary text-xs font-bold whitespace-nowrap disabled:opacity-50">
                  {saving ? "Inviting…" : "Send Invite"}
                </button>
              </div>
            </div>
            <div className="space-y-1.5">
              <p className="text-[10px] font-black uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">
                Test Access — select which phases this user can run
              </p>
              <PhasePicker value={newRole} onChange={setNewRole} />
            </div>
          </div>
        )}

        <div className="divide-y divide-[var(--color-outline-variant)]/10">
          {teamMembers.length === 0 ? (
            <div className="p-12 text-center space-y-2">
              <span className="material-symbols-outlined text-3xl text-[var(--color-outline)] opacity-40">group_add</span>
              <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">No team members yet. Invite your first member above.</p>
            </div>
          ) : teamMembers.map(u => {
            const userPct = u.run_limit > 0 ? Math.round((u.runs_used / u.run_limit) * 100) : 0;
            const isExpanded = expandedUser === u.user_email;
            return (
              <div key={u.user_email} className="border-b border-[var(--color-outline-variant)]/10 last:border-0">
                <div className="p-4 flex items-center gap-4 flex-wrap">
                  {/* Avatar + email */}
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

                  {/* Current permissions badge — click to expand picker */}
                  <button
                    onClick={() => setExpandedUser(isExpanded ? null : u.user_email)}
                    className={`flex items-center gap-1.5 text-[11px] font-bold px-2.5 py-1 rounded-full transition-all ${roleColor(u.role)} hover:opacity-80`}
                  >
                    <span className="material-symbols-outlined text-xs">tune</span>
                    {roleLabel(u.role)}
                  </button>

                  {/* Run limit */}
                  <div className="flex items-center gap-1.5">
                    <span className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">Limit:</span>
                    <input defaultValue={u.run_limit} type="number" min={0}
                      aria-label="Run limit"
                      placeholder="0"
                      onBlur={e => updateUser(u.user_email, "run_limit", Number(e.target.value))}
                      className="w-16 text-xs text-center px-2 py-1 rounded-lg border border-[var(--color-outline-variant)] bg-white" />
                  </div>

                  {/* Remove */}
                  <button onClick={() => removeUser(u.user_email)}
                    className="w-8 h-8 rounded-lg flex items-center justify-center text-[var(--color-outline)] hover:text-error hover:bg-error/10 transition-all">
                    <span className="material-symbols-outlined text-base">person_remove</span>
                  </button>
                </div>

                {/* Inline phase picker — expands when badge is clicked */}
                {isExpanded && (
                  <div className="px-5 pb-4 pt-1 bg-[var(--color-surface-container-low)] space-y-2">
                    <p className="text-[10px] font-black uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">
                      Edit test access for {u.user_email}
                    </p>
                    <PhasePicker
                      value={u.role}
                      onChange={role => {
                        updateUser(u.user_email, "role", role);
                        setExpandedUser(null);
                      }}
                    />
                  </div>
                )}
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
