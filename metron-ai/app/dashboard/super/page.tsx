"use client";

import { useState, useEffect, useCallback } from "react";
import { authFetch } from "@/lib/api";
import { ProgressFill } from "@/components/ProgressFill";

interface Tenant {
  tenant_id: string;
  name: string;
  quota_limit: number;
  quota_used: number;
  user_count: number;
  run_count: number;
  created_at: string;
}

interface TenantDetail {
  tenant_id: string;
  name: string;
  quota_limit: number;
  quota_used: number;
  users: { user_email: string; role: string; run_limit: number; runs_used: number }[];
}

export default function SuperAdminPage() {
  const [tenants, setTenants] = useState<Tenant[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [selected, setSelected] = useState<TenantDetail | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);

  // Create tenant form
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState("");
  const [newQuota, setNewQuota] = useState(50);
  const [creating, setCreating] = useState(false);

  // Edit quota inline
  const [editQuota, setEditQuota] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const r = await authFetch("/api/super/tenants");
      if (r.status === 403) { setError("Super admin access required."); return; }
      if (!r.ok) { setError("Failed to load tenants."); return; }
      const d = await r.json();
      setTenants(d.tenants || []);
    } catch { setError("Network error."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

  const openDetail = async (tenant_id: string) => {
    setDetailLoading(true);
    const r = await authFetch(`/api/super/tenants/${tenant_id}`);
    if (r.ok) setSelected(await r.json());
    setDetailLoading(false);
  };

  const createTenant = async () => {
    if (!newName.trim()) return;
    setCreating(true);
    const r = await authFetch("/api/super/tenants", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: newName.trim(), quota_limit: newQuota }),
    });
    setCreating(false);
    if (r.ok) {
      setNewName(""); setNewQuota(50); setShowCreate(false);
      load();
    }
  };

  const saveQuota = async (tenant_id: string) => {
    const newVal = editQuota[tenant_id];
    if (newVal === undefined) return;
    await authFetch(`/api/super/tenants/${tenant_id}`, {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ quota_limit: newVal }),
    });
    load();
    if (selected?.tenant_id === tenant_id) openDetail(tenant_id);
  };

  const resetQuota = async (tenant_id: string) => {
    if (!confirm("Reset this tenant's quota_used to 0?")) return;
    await authFetch(`/api/super/tenants/${tenant_id}/reset-quota`, { method: "POST" });
    load();
    if (selected?.tenant_id === tenant_id) openDetail(tenant_id);
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

  const totalUsers = tenants.reduce((s, t) => s + t.user_count, 0);
  const totalRuns  = tenants.reduce((s, t) => s + t.run_count, 0);

  return (
    <div className="max-w-5xl mx-auto space-y-8 animate-fade-in">
      {/* Header */}
      <div className="space-y-1">
        <div className="flex items-center gap-2">
          <span className="w-1.5 h-1.5 rounded-full bg-error" />
          <span className="text-[10px] font-black uppercase tracking-[0.2em] text-error">Super Admin</span>
        </div>
        <h1 className="font-headline text-4xl font-black tracking-tighter">All Tenants</h1>
        <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">
          Manage all client organizations and their quotas.
        </p>
      </div>

      {/* Summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {[
          { label: "Tenants", value: tenants.length, icon: "corporate_fare" },
          { label: "Total Users", value: totalUsers, icon: "group" },
          { label: "Total Runs", value: totalRuns, icon: "history" },
          { label: "Active", value: tenants.filter(t => t.quota_used > 0).length, icon: "bolt" },
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

      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">
        {/* Left: tenant list */}
        <div className="card overflow-hidden">
          <div className="flex items-center justify-between p-5 border-b border-[var(--color-outline-variant)]/20">
            <h2 className="font-headline font-black text-lg">Clients</h2>
            <button onClick={() => setShowCreate(!showCreate)}
              className="flex items-center gap-2 px-4 py-2 rounded-xl btn-primary text-xs font-bold">
              <span className="material-symbols-outlined text-sm">add</span>
              New Tenant
            </button>
          </div>

          {/* Create form */}
          {showCreate && (
            <div className="p-4 border-b border-[var(--color-outline-variant)]/20 bg-[var(--color-surface-container-low)] space-y-3">
              <p className="text-[10px] font-black uppercase tracking-wider text-primary">New Tenant</p>
              <div className="flex gap-2">
                <input value={newName} onChange={e => setNewName(e.target.value)}
                  placeholder="Company name"
                  className="flex-1 px-3 py-2 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white" />
                <input value={newQuota} type="number" min={1} onChange={e => setNewQuota(Number(e.target.value))}
                  placeholder="Quota"
                  className="w-20 px-3 py-2 rounded-xl border border-[var(--color-outline-variant)] text-sm bg-white text-center" />
                <button onClick={createTenant} disabled={creating}
                  className="px-4 py-2 rounded-xl btn-primary text-xs font-bold whitespace-nowrap disabled:opacity-50">
                  {creating ? "…" : "Create"}
                </button>
              </div>
            </div>
          )}

          <div className="divide-y divide-[var(--color-outline-variant)]/10 max-h-[60vh] overflow-y-auto">
            {tenants.length === 0 ? (
              <div className="p-8 text-center text-sm text-[var(--color-on-surface-variant)] opacity-60">
                No tenants yet. Create your first client above.
              </div>
            ) : tenants.map(t => {
              const pct = t.quota_limit > 0 ? Math.round((t.quota_used / t.quota_limit) * 100) : 0;
              const isSelected = selected?.tenant_id === t.tenant_id;
              return (
                <button key={t.tenant_id} onClick={() => openDetail(t.tenant_id)}
                  className={`w-full text-left p-4 transition-all hover:bg-[var(--color-surface-container-low)] ${isSelected ? "bg-primary/5 border-l-2 border-primary" : ""}`}>
                  <div className="flex items-center justify-between mb-1.5">
                    <p className="text-sm font-black">{t.name}</p>
                    <span className={`text-[10px] font-bold px-2 py-0.5 rounded-full ${pct >= 100 ? "bg-error/10 text-error" : pct >= 80 ? "bg-[#855300]/10 text-[#855300]" : "bg-secondary/10 text-secondary"}`}>
                      {pct}%
                    </span>
                  </div>
                  <div className="h-1 rounded-full bg-[var(--color-outline-variant)]/30 overflow-hidden mb-2">
                    <ProgressFill pct={pct} className={`h-full rounded-full ${pct >= 100 ? "bg-error" : pct >= 80 ? "bg-[#855300]" : "bg-primary"}`} />
                  </div>
                  <div className="flex items-center gap-4 text-[10px] text-[var(--color-on-surface-variant)] opacity-60">
                    <span><span className="font-bold text-[var(--color-on-surface)] opacity-100">{t.user_count}</span> users</span>
                    <span><span className="font-bold text-[var(--color-on-surface)] opacity-100">{t.quota_used}/{t.quota_limit > 0 ? t.quota_limit : "∞"}</span> runs</span>
                    <span><span className="font-bold text-[var(--color-on-surface)] opacity-100">{t.run_count}</span> all-time</span>
                  </div>
                </button>
              );
            })}
          </div>
        </div>

        {/* Right: tenant detail */}
        <div className="card overflow-hidden">
          {detailLoading ? (
            <div className="flex items-center justify-center py-20">
              <span className="material-symbols-outlined animate-spin text-primary">progress_activity</span>
            </div>
          ) : !selected ? (
            <div className="flex flex-col items-center justify-center py-20 gap-3 text-[var(--color-on-surface-variant)] opacity-40">
              <span className="material-symbols-outlined text-4xl">corporate_fare</span>
              <p className="text-sm">Select a tenant to view details</p>
            </div>
          ) : (
            <>
              <div className="p-5 border-b border-[var(--color-outline-variant)]/20 space-y-3">
                <div className="flex items-center justify-between">
                  <h3 className="font-headline font-black text-xl">{selected.name}</h3>
                  <button onClick={() => resetQuota(selected.tenant_id)}
                    className="text-[10px] font-bold px-3 py-1.5 rounded-lg border border-[var(--color-outline-variant)] hover:border-error hover:text-error transition-all">
                    Reset Quota
                  </button>
                </div>
                {/* Quota editor */}
                <div className="flex items-center gap-3">
                  <span className="text-xs text-[var(--color-on-surface-variant)] opacity-60">Monthly quota:</span>
                  <input
                    defaultValue={selected.quota_limit}
                    type="number" min={0}
                    aria-label="Monthly quota"
                    placeholder="0"
                    onChange={e => setEditQuota(prev => ({ ...prev, [selected.tenant_id]: Number(e.target.value) }))}
                    className="w-20 text-xs text-center px-2 py-1 rounded-lg border border-[var(--color-outline-variant)] bg-white"
                  />
                  <button onClick={() => saveQuota(selected.tenant_id)}
                    className="text-[10px] font-bold px-3 py-1.5 rounded-lg btn-primary">
                    Save
                  </button>
                  <span className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">
                    used: {selected.quota_used}
                  </span>
                </div>
              </div>

              {/* Users */}
              <div className="p-4 space-y-2 max-h-[50vh] overflow-y-auto">
                <p className="text-[10px] font-black uppercase tracking-wider text-[var(--color-on-surface-variant)] opacity-60 mb-3">
                  Users ({selected.users.length})
                </p>
                {selected.users.length === 0 ? (
                  <p className="text-sm text-[var(--color-on-surface-variant)] opacity-40 text-center py-4">No users yet.</p>
                ) : selected.users.map(u => (
                  <div key={u.user_email} className="flex items-center gap-3 p-2 rounded-xl bg-[var(--color-surface-container-low)]">
                    <div className="w-8 h-8 rounded-full bg-gradient-to-br from-[#00668a] to-[#38bdf8] flex items-center justify-center text-white text-[10px] font-bold flex-shrink-0">
                      {u.user_email[0].toUpperCase()}
                    </div>
                    <div className="flex-1 min-w-0">
                      <p className="text-xs font-bold truncate">{u.user_email}</p>
                      <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60">
                        {u.role.replace(/_/g, " ")} · {u.runs_used}/{u.run_limit > 0 ? u.run_limit : "∞"} runs
                      </p>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}
