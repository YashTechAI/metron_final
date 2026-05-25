"use client";

import { useState, useEffect, useCallback } from "react";
import { authFetch } from "@/lib/api";

interface Run {
  run_id: string;
  user_email: string;
  project_id: string;
  timestamp: string;
  health_score: number | null;
  domain: string;
  application_type: string;
  status: string;
}

export default function AdminRunsPage() {
  const [runs, setRuns] = useState<Run[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  const load = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const r = await authFetch("/api/admin/runs");
      if (!r.ok) { setError("Failed to load runs."); return; }
      const d = await r.json();
      setRuns(d.runs || []);
    } catch { setError("Network error."); }
    finally { setLoading(false); }
  }, []);

  useEffect(() => { load(); }, [load]);

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

  return (
    <div className="max-w-4xl mx-auto space-y-6 animate-fade-in">
      <div className="space-y-1">
        <h1 className="font-headline text-4xl font-black tracking-tighter">All Runs</h1>
        <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">
          Every test run across your organisation — {runs.length} total.
        </p>
      </div>

      <div className="card overflow-hidden">
        {runs.length === 0 ? (
          <div className="p-12 text-center space-y-2">
            <span className="material-symbols-outlined text-3xl text-[var(--color-outline)] opacity-40">history</span>
            <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">No runs yet across your organisation.</p>
          </div>
        ) : (
          <div className="divide-y divide-[var(--color-outline-variant)]/10">
            {runs.map(run => {
              const score = run.health_score != null ? Math.round(run.health_score * 100) : null;
              const date = new Date(run.timestamp + "Z").toLocaleString();
              return (
                <div key={run.run_id} className="p-4 flex items-center gap-4 flex-wrap">
                  {/* User avatar */}
                  <div className="w-9 h-9 rounded-full bg-gradient-to-br from-[#00668a] to-[#38bdf8] flex items-center justify-center text-white text-xs font-black flex-shrink-0">
                    {run.user_email[0].toUpperCase()}
                  </div>

                  {/* User + project info */}
                  <div className="flex-1 min-w-0">
                    <p className="text-sm font-bold truncate">{run.user_email}</p>
                    <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-60 truncate">
                      {date} · {run.domain || "General"} · {run.application_type || "AI System"}
                    </p>
                  </div>

                  {/* Score */}
                  <div className="text-right">
                    <p className="text-[9px] font-black text-[var(--color-outline)] uppercase tracking-widest">Health</p>
                    <p className={`text-xl font-black font-headline ${
                      score == null ? "text-[var(--color-outline)]"
                      : score >= 75 ? "text-[#006e2f]"
                      : score >= 50 ? "text-amber-600"
                      : "text-red-600"
                    }`}>
                      {score != null ? `${score}%` : "---"}
                    </p>
                  </div>

                  {/* Status badge */}
                  <span className={`text-[10px] font-black px-2.5 py-1 rounded-full uppercase tracking-wider ${
                    run.status === "completed" ? "bg-secondary/10 text-secondary"
                    : run.status === "running" ? "bg-primary/10 text-primary"
                    : "bg-error/10 text-error"
                  }`}>
                    {run.status || "unknown"}
                  </span>
                </div>
              );
            })}
          </div>
        )}
      </div>
    </div>
  );
}
