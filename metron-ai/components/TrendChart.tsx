"use client";

import { useState, useEffect } from "react";
import {
  LineChart, Line, XAxis, YAxis, CartesianGrid, Tooltip,
  ResponsiveContainer, Legend, ReferenceLine,
} from "recharts";
import { authFetch } from "@/lib/api";

interface TrendPoint {
  run_id: string;
  mlflow_run_id: string;
  timestamp: number;
  health_score: number | null;
  p95_latency_ms: number | null;
  tokens_input: number | null;
  tokens_output: number | null;
  cost_usd: number | null;
  regression_detected: boolean;
  prompt_changed: boolean;
  prompt_hash: string;
}

interface CostData {
  run_id: string;
  tokens_input: number;
  tokens_output: number;
  total_tokens: number;
  estimated_cost_usd: number;
  mlflow_enabled: boolean;
}

interface Props {
  projectId: string;
  currentRunId: string;
}

// Custom dot: red for regression, yellow for prompt change, default otherwise
function RegressionDot(props: {
  cx?: number; cy?: number;
  payload?: TrendPoint;
  currentRunId: string;
}) {
  const { cx = 0, cy = 0, payload, currentRunId } = props;
  if (!payload) return null;
  const isCurrentRun = payload.run_id === currentRunId;
  const color = payload.regression_detected ? "#ef4444"
    : payload.prompt_changed ? "#f59e0b"
    : isCurrentRun ? "#00668a"
    : "#94a3b8";
  return <circle cx={cx} cy={cy} r={isCurrentRun ? 6 : 4} fill={color} stroke="white" strokeWidth={1.5} />;
}

function HealthDot(props: { cx?: number; cy?: number; payload?: TrendPoint; currentRunId: string }) {
  return <RegressionDot {...props} />;
}

function LatencyDot(props: { cx?: number; cy?: number; payload?: TrendPoint; currentRunId: string }) {
  const { cx = 0, cy = 0, payload, currentRunId } = props;
  if (!payload) return null;
  const isCurrentRun = payload.run_id === currentRunId;
  const color = isCurrentRun ? "#6366f1" : "#94a3b8";
  return <circle cx={cx} cy={cy} r={isCurrentRun ? 6 : 4} fill={color} stroke="white" strokeWidth={1.5} />;
}

function CustomTooltip({ active, payload }: {
  active?: boolean;
  payload?: Array<{ value: number; name: string; payload: TrendPoint }>;
}) {
  if (!active || !payload?.length) return null;
  const pt = payload[0].payload;
  const date = new Date(pt.timestamp).toLocaleDateString("en-US", {
    month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
  });
  return (
    <div className="bg-white border border-[var(--color-outline-variant)] rounded-xl p-3 shadow-lg text-xs space-y-1 min-w-[160px]">
      <p className="font-bold text-[var(--color-on-surface)] text-[11px]">{date}</p>
      {payload.map((p, i) => (
        <div key={i} className="flex items-center justify-between gap-4">
          <span className="text-[var(--color-on-surface-variant)]">{p.name}</span>
          <span className="font-black">{p.value?.toFixed(p.name === "Health" ? 1 : 0)}{p.name === "Health" ? "%" : "ms"}</span>
        </div>
      ))}
      {pt.cost_usd != null && (
        <div className="flex items-center justify-between gap-4 pt-1 border-t border-[var(--color-outline-variant)]">
          <span className="text-[var(--color-on-surface-variant)]">Cost</span>
          <span className="font-black">${pt.cost_usd.toFixed(4)}</span>
        </div>
      )}
      {pt.regression_detected && (
        <p className="text-red-500 font-bold pt-1">Regression detected</p>
      )}
      {pt.prompt_changed && (
        <p className="text-amber-500 font-bold">Prompt changed</p>
      )}
    </div>
  );
}

export default function TrendChart({ projectId, currentRunId }: Props) {
  const [trend, setTrend] = useState<TrendPoint[]>([]);
  const [cost, setCost] = useState<CostData | null>(null);
  const [mlflowEnabled, setMlflowEnabled] = useState<boolean | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");

  useEffect(() => {
    const loadData = async () => {
      try {
        const [trendRes, costRes] = await Promise.all([
          authFetch(`/api/project/${projectId}/trend?limit=30`),
          authFetch(`/api/project/${projectId}/runs/${currentRunId}/cost`),
        ]);

        if (trendRes.ok) {
          const trendData = await trendRes.json();
          setMlflowEnabled(trendData.mlflow_enabled);
          setTrend(trendData.trend || []);
        }

        if (costRes.ok) {
          const costData = await costRes.json();
          setCost(costData);
        }
      } catch (e) {
        setError(e instanceof Error ? e.message : "Failed to load trend data");
      } finally {
        setLoading(false);
      }
    };
    loadData();
  }, [projectId, currentRunId]);

  if (loading) {
    return (
      <div className="flex items-center justify-center h-48 gap-3">
        <span className="material-symbols-outlined animate-spin text-primary">progress_activity</span>
        <span className="text-sm text-[var(--color-on-surface-variant)]">Loading trend data…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="py-10 text-center space-y-2">
        <span className="material-symbols-outlined text-3xl text-error">error</span>
        <p className="text-sm text-[var(--color-on-surface-variant)]">{error}</p>
      </div>
    );
  }

  if (mlflowEnabled === false) {
    return (
      <div className="py-12 text-center space-y-3">
        <span className="material-symbols-outlined text-4xl text-[var(--color-on-surface-variant)] opacity-30">trending_up</span>
        <p className="text-sm font-semibold text-[var(--color-on-surface-variant)]">MLflow not configured</p>
        <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 max-w-sm mx-auto">
          Set <code className="font-mono bg-[var(--color-surface-container-low)] px-1 rounded">MLFLOW_TRACKING_URI</code> on your backend to enable trend tracking, cost monitoring, and regression detection.
        </p>
      </div>
    );
  }

  // Find current run in trend for banners
  const currentPoint = trend.find(p => p.run_id === currentRunId);

  // Chart data: health as 0-100, latency as-is
  const chartData = trend.map(p => ({
    ...p,
    health_pct: p.health_score != null ? Math.round(p.health_score * 100) : null,
    date: new Date(p.timestamp).toLocaleDateString("en-US", { month: "short", day: "numeric" }),
  }));

  return (
    <div className="space-y-6">
      {/* Regression / prompt change banners */}
      {currentPoint?.regression_detected && (
        <div className="flex items-start gap-3 p-4 rounded-xl border border-red-200 bg-red-50">
          <span className="material-symbols-outlined text-red-500 text-xl flex-shrink-0">trending_down</span>
          <div>
            <p className="text-sm font-black text-red-700">Regression detected in this run</p>
            <p className="text-xs text-red-600 mt-0.5">Health score dropped compared to the previous run.</p>
          </div>
        </div>
      )}
      {currentPoint?.prompt_changed && !currentPoint?.regression_detected && (
        <div className="flex items-start gap-3 p-4 rounded-xl border border-amber-200 bg-amber-50">
          <span className="material-symbols-outlined text-amber-500 text-xl flex-shrink-0">edit_note</span>
          <div>
            <p className="text-sm font-black text-amber-700">Agent description changed</p>
            <p className="text-xs text-amber-600 mt-0.5">The agent description or domain is different from the previous run.</p>
          </div>
        </div>
      )}

      {/* Cost card for current run */}
      {cost?.mlflow_enabled && (
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Tokens In",  value: (cost.tokens_input ?? 0).toLocaleString(),  icon: "input" },
            { label: "Tokens Out", value: (cost.tokens_output ?? 0).toLocaleString(), icon: "output" },
            { label: "Total Tokens", value: (cost.total_tokens ?? 0).toLocaleString(), icon: "data_usage" },
            { label: "Est. Cost", value: `$${(cost.estimated_cost_usd ?? 0).toFixed(4)}`, icon: "payments" },
          ].map(({ label, value, icon }) => (
            <div key={label} className="p-4 rounded-xl bg-[var(--color-surface-container-low)] text-center">
              <span className="material-symbols-outlined text-xl text-primary">{icon}</span>
              <p className="font-headline text-xl font-black text-[var(--color-on-surface)] mt-1">{value}</p>
              <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">{label}</p>
            </div>
          ))}
        </div>
      )}

      {/* Trend chart */}
      {trend.length === 0 ? (
        <div className="py-10 text-center space-y-2">
          <span className="material-symbols-outlined text-3xl text-[var(--color-on-surface-variant)] opacity-30">show_chart</span>
          <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">No historical runs yet. Trend will appear after multiple runs.</p>
        </div>
      ) : (
        <div className="space-y-3">
          <p className="text-[10px] font-black uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">
            Health & Latency Trend — last {trend.length} run{trend.length !== 1 ? "s" : ""}
          </p>

          {/* Legend */}
          <div className="flex flex-wrap gap-4 text-xs">
            <div className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-full bg-[#00668a]" />
              <span className="text-[var(--color-on-surface-variant)]">Health Score (%)</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-full bg-[#6366f1]" />
              <span className="text-[var(--color-on-surface-variant)]">P95 Latency (ms)</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-full bg-red-500" />
              <span className="text-[var(--color-on-surface-variant)]">Regression</span>
            </div>
            <div className="flex items-center gap-1.5">
              <span className="w-3 h-3 rounded-full bg-amber-400" />
              <span className="text-[var(--color-on-surface-variant)]">Prompt Changed</span>
            </div>
          </div>

          <ResponsiveContainer width="100%" height={300}>
            <LineChart data={chartData} margin={{ top: 8, right: 16, bottom: 8, left: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="rgba(0,0,0,0.06)" />
              <XAxis
                dataKey="date"
                tick={{ fontSize: 10, fill: "var(--color-on-surface-variant)" }}
                tickLine={false}
              />
              <YAxis
                yAxisId="health"
                domain={[0, 100]}
                tick={{ fontSize: 10, fill: "var(--color-on-surface-variant)" }}
                tickLine={false}
                axisLine={false}
                tickFormatter={v => `${v}%`}
              />
              <YAxis
                yAxisId="latency"
                orientation="right"
                tick={{ fontSize: 10, fill: "var(--color-on-surface-variant)" }}
                tickLine={false}
                axisLine={false}
                tickFormatter={v => `${v}ms`}
              />
              <Tooltip content={<CustomTooltip />} />
              {/* 70% health threshold line */}
              <ReferenceLine yAxisId="health" y={70} stroke="#ef4444" strokeDasharray="4 4" strokeOpacity={0.5} />

              <Line
                yAxisId="health"
                type="monotone"
                dataKey="health_pct"
                name="Health"
                stroke="#00668a"
                strokeWidth={2}
                dot={(props) => <HealthDot {...props} currentRunId={currentRunId} />}
                activeDot={false}
                connectNulls
              />
              <Line
                yAxisId="latency"
                type="monotone"
                dataKey="p95_latency_ms"
                name="P95 Latency"
                stroke="#6366f1"
                strokeWidth={2}
                strokeDasharray="5 3"
                dot={(props) => <LatencyDot {...props} currentRunId={currentRunId} />}
                activeDot={false}
                connectNulls
              />
            </LineChart>
          </ResponsiveContainer>

          <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-50">
            Red dashed line = 70% health threshold · Highlighted dot = current run
          </p>
        </div>
      )}
    </div>
  );
}
