"use client";

import { useState, useEffect, Suspense, useCallback } from "react";
import { useParams, useRouter, useSearchParams } from "next/navigation";
import { authFetch } from "@/lib/api";

const API = "";

// ── Metric display name mapping ───────────────────────────────────────────────
const METRIC_LABELS: Record<string, string> = {
  // Functional
  hallucination:                "Hallucination",
  answer_relevancy:             "Answer Relevancy",
  usefulness:                   "Usefulness",
  llm_judge:                    "LLM Judge",
  // Functional — per-turn LLM judge criteria
  llm_relevance:                "LLM Relevance",
  llm_accuracy:                 "LLM Accuracy",
  llm_helpfulness:              "LLM Helpfulness",
  llm_completeness:             "LLM Completeness",
  llm_answer_similarity:        "LLM Answer Similarity",
  // Functional — cross-turn evaluation
  cross_turn_consistency:       "Cross-Turn Consistency",
  cross_turn_context_awareness: "Cross-Turn Context Awareness",
  // Functional — GEval
  factual_accuracy:             "Factual Accuracy",
  // Security
  pii_leakage:                  "PII Leakage",
  toxicity:                     "Toxicity (Output)",
  prompt_injection:             "Prompt Injection",
  bias_fairness:                "Bias & Fairness",
  toxic_request:                "Toxic Request",
  attack_resistance:            "Attack Resistance",
  // Quality
  geval_overall:                "GEval Overall",
  ragas_faithfulness:           "Faithfulness (RAGAS)",
  ragas_answer_relevancy:       "Answer Relevancy (RAGAS)",
  ragas_context_recall:         "Context Recall (RAGAS)",
  ragas_context_precision:      "Context Precision (RAGAS)",
  // RAG evaluation — RAGAS
  rag_faithfulness:             "Faithfulness (RAGAS)",
  rag_context_recall:           "Context Recall (RAGAS)",
  rag_context_precision:        "Context Precision (RAGAS)",
  // RAG evaluation — DeepEval
  rag_answer_relevancy:         "Answer Relevancy (DeepEval)",
  rag_context_relevancy:        "Context Relevancy (DeepEval)",
};

function metricLabel(name: string): string {
  if (METRIC_LABELS[name]) return METRIC_LABELS[name];
  if (name.startsWith("geval_")) {
    return "GEval " + name.slice(6).replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
  }
  return name.replace(/_/g, " ").replace(/\b\w/g, c => c.toUpperCase());
}

// Security metrics: detection = pass means no issue found; resistance = pass means attack was blocked
function isDetectionMetric(name: string) { return ["pii_leakage", "toxicity", "bias_fairness"].includes(name); }
function isResistanceMetric(name: string) { return ["prompt_injection", "attack_resistance", "toxic_request"].includes(name); }

// ─────────────────────────────────── Types ────────────────────────────────────
interface TestResult {
  test_id: string;
  test_name: string;
  category: string;
  input_text: string;
  output_text: string;
  score: number;
  passed: boolean;
  reasoning: string;
  latency_ms: number;
  failure_taxonomy_id?: string;
  failure_taxonomy_label?: string;
  failure_reason?: string;
  details: Record<string, unknown>;
  manually_passed?: boolean;
  original_score?: number;
  original_passed?: boolean;
}

interface PhaseSummary {
  total: number;
  passed: number;
  failed: number;
  skipped?: number;
  pass_rate: number;
  avg_score: number;
  evaluation_warnings?: string[];
  results: TestResult[];
}

interface PerformanceMetrics {
  total_requests: number;
  successful: number;
  errors: number;
  error_rate: number;
  min_latency: number;
  avg_latency: number;
  median_latency: number;
  p95_latency: number;
  p99_latency: number;
  max_latency: number;
  throughput: number;
}

interface LoadMetrics {
  concurrent_users: number;
  duration_seconds: number;
  total_requests: number;
  successful: number;
  errors: number;
  error_rate: number;
  avg_latency: number;
  p95_latency: number;
  requests_per_second: number;
  tool_used: string;
}


interface StageTotals { calls: number; total_tokens: number; cost_usd: number; }
interface TokenSummary {
  total_calls: number;
  total_prompt_tokens: number;
  total_completion_tokens: number;
  total_tokens: number;
  estimated_cost_usd: number;
  avg_latency_ms: number;
  tpot_ms: number;
  token_efficiency_ratio: number;
  tpm_velocity: number;
  retry_count: number;
  truncated_calls: number;
  truncation_rate: number;
  models_used: Record<string, number>;
  by_stage: Record<string, StageTotals>;
  mlflow_run_id?: string;
}

interface FullResults {
  run_id: string;
  health_score: number;
  passed: boolean;
  user_role?: string;
  domain: string;
  agent_name: string;
  config_summary: Record<string, unknown>;
  functional: PhaseSummary;
  security: PhaseSummary;
  quality: PhaseSummary;
  rag?: PhaseSummary;
  performance: PerformanceMetrics;
  load: LoadMetrics;
  personas: Array<{ id: string; name: string; description: string; traits: string[] }>;
  persona_breakdown: Array<{
    persona_id: string;
    persona_name: string;
    total: number;
    passed: number;
    avg_score: number;
    pass_rate: number;
    intent?: string;
    fishbone?: Record<string, string>;
  }>;
  failure_drill_down: TestResult[];
  total_tests: number;
  total_passed: number;
  total_skipped?: number;
  evaluation_warnings?: string[];
  report_html?: string;
}

// ── Domain weights (mirrors backend config.py DOMAIN_WEIGHTS) ──────────────────
const DOMAIN_WEIGHTS: Record<string, Record<string, number>> = {
  finance:          { functional: 0.35, security: 0.40, quality: 0.10, performance: 0.10, load: 0.05 },
  banking:          { functional: 0.35, security: 0.40, quality: 0.10, performance: 0.10, load: 0.05 },
  medical:          { functional: 0.35, security: 0.45, quality: 0.10, performance: 0.08, load: 0.02 },
  healthcare:       { functional: 0.35, security: 0.45, quality: 0.10, performance: 0.08, load: 0.02 },
  legal:            { functional: 0.35, security: 0.40, quality: 0.15, performance: 0.07, load: 0.03 },
  travel:           { functional: 0.35, security: 0.15, quality: 0.10, performance: 0.25, load: 0.15 },
  ecommerce:        { functional: 0.35, security: 0.20, quality: 0.10, performance: 0.20, load: 0.15 },
  retail:           { functional: 0.35, security: 0.20, quality: 0.10, performance: 0.20, load: 0.15 },
  hr:               { functional: 0.35, security: 0.35, quality: 0.15, performance: 0.10, load: 0.05 },
  human_resources:  { functional: 0.35, security: 0.35, quality: 0.15, performance: 0.10, load: 0.05 },
  education:        { functional: 0.40, security: 0.15, quality: 0.30, performance: 0.10, load: 0.05 },
  support:          { functional: 0.40, security: 0.15, quality: 0.15, performance: 0.20, load: 0.10 },
  customer_support: { functional: 0.40, security: 0.15, quality: 0.15, performance: 0.20, load: 0.10 },
  email:            { functional: 0.45, security: 0.15, quality: 0.25, performance: 0.10, load: 0.05 },
  government:       { functional: 0.30, security: 0.45, quality: 0.15, performance: 0.07, load: 0.03 },
  _default:         { functional: 0.40, security: 0.30, quality: 0.10, performance: 0.15, load: 0.05 },
};

function getWeights(domain: string) {
  return DOMAIN_WEIGHTS[domain?.toLowerCase()] ?? DOMAIN_WEIGHTS._default;
}

function applyManualPass(
  prev: FullResults,
  phase: "functional" | "security" | "quality" | "rag",
  testId: string,
): FullResults {
  const phaseData = prev[phase];
  if (!phaseData) return prev;

  const newResults = phaseData.results.map(r =>
    r.test_id === testId ? {
      ...r,
      score: 1.0,
      passed: true,
      manually_passed: true,
      original_score: r.manually_passed ? r.original_score : r.score,
      original_passed: r.manually_passed ? r.original_passed : r.passed,
    } : r
  );
  const newPassed   = newResults.filter(r => r.passed).length;
  const newFailed   = newResults.length - newPassed;
  const newAvgScore = newResults.length > 0 ? newResults.reduce((s, r) => s + r.score, 0) / newResults.length : 0;
  const newPassRate = newResults.length > 0 ? Math.round((newPassed / newResults.length) * 100) : 0;
  const newPhaseData: PhaseSummary = { ...phaseData, results: newResults, passed: newPassed, failed: newFailed, avg_score: newAvgScore, pass_rate: newPassRate };

  const weights      = getWeights(prev.domain);
  const totalWeight  = Object.values(weights).reduce((a, b) => a + b, 0);
  // pass_rate is stored as 0-100 in frontend; backend health uses 0-1 scale
  const funcPR       = phase === "functional" ? newPassRate / 100 : (prev.functional?.pass_rate ?? 0) / 100;
  const secPR        = phase === "security"   ? newPassRate / 100 : (prev.security?.pass_rate  ?? 0) / 100;
  const qualPR       = phase === "quality"    ? newPassRate / 100 : (prev.quality?.pass_rate   ?? 0) / 100;
  const oldContrib   = (prev.functional?.pass_rate ?? 0) / 100 * (weights.functional ?? 0)
                     + (prev.security?.pass_rate   ?? 0) / 100 * (weights.security   ?? 0)
                     + (prev.quality?.pass_rate    ?? 0) / 100 * (weights.quality    ?? 0);
  const perfLoad     = prev.health_score * totalWeight - oldContrib;
  const newHealth    = Math.min(1, Math.max(0,
    (funcPR * (weights.functional ?? 0) + secPR * (weights.security ?? 0) + qualPR * (weights.quality ?? 0) + perfLoad) / totalWeight
  ));

  const allResults = [
    ...(phase === "functional" ? newResults : prev.functional?.results ?? []),
    ...(phase === "security"   ? newResults : prev.security?.results   ?? []),
    ...(phase === "quality"    ? newResults : prev.quality?.results    ?? []),
    ...(phase === "rag"        ? newResults : prev.rag?.results        ?? []),
  ];

  return { ...prev, [phase]: newPhaseData, health_score: newHealth, passed: newHealth >= 0.50, total_passed: allResults.filter(r => r.passed).length };
}

function applyManualRevert(
  prev: FullResults,
  phase: "functional" | "security" | "quality" | "rag",
  testId: string,
): FullResults {
  const phaseData = prev[phase];
  if (!phaseData) return prev;

  const newResults = phaseData.results.map(r =>
    r.test_id === testId ? {
      ...r,
      score: r.original_score ?? r.score,
      passed: r.original_passed ?? r.passed,
      manually_passed: false,
      original_score: undefined,
      original_passed: undefined,
    } : r
  );
  const newPassed   = newResults.filter(r => r.passed).length;
  const newFailed   = newResults.length - newPassed;
  const newAvgScore = newResults.length > 0 ? newResults.reduce((s, r) => s + r.score, 0) / newResults.length : 0;
  const newPassRate = newResults.length > 0 ? Math.round((newPassed / newResults.length) * 100) : 0;
  const newPhaseData: PhaseSummary = { ...phaseData, results: newResults, passed: newPassed, failed: newFailed, avg_score: newAvgScore, pass_rate: newPassRate };

  const weights     = getWeights(prev.domain);
  const totalWeight = Object.values(weights).reduce((a, b) => a + b, 0);
  // pass_rate is stored as 0-100 in frontend; backend health uses 0-1 scale
  const funcPR      = phase === "functional" ? newPassRate / 100 : (prev.functional?.pass_rate ?? 0) / 100;
  const secPR       = phase === "security"   ? newPassRate / 100 : (prev.security?.pass_rate  ?? 0) / 100;
  const qualPR      = phase === "quality"    ? newPassRate / 100 : (prev.quality?.pass_rate   ?? 0) / 100;
  const oldContrib  = (prev.functional?.pass_rate ?? 0) / 100 * (weights.functional ?? 0)
                    + (prev.security?.pass_rate   ?? 0) / 100 * (weights.security   ?? 0)
                    + (prev.quality?.pass_rate    ?? 0) / 100 * (weights.quality    ?? 0);
  const perfLoad    = prev.health_score * totalWeight - oldContrib;
  const newHealth   = Math.min(1, Math.max(0,
    (funcPR * (weights.functional ?? 0) + secPR * (weights.security ?? 0) + qualPR * (weights.quality ?? 0) + perfLoad) / totalWeight
  ));

  const allResults = [
    ...(phase === "functional" ? newResults : prev.functional?.results ?? []),
    ...(phase === "security"   ? newResults : prev.security?.results   ?? []),
    ...(phase === "quality"    ? newResults : prev.quality?.results    ?? []),
    ...(phase === "rag"        ? newResults : prev.rag?.results        ?? []),
  ];

  return { ...prev, [phase]: newPhaseData, health_score: newHealth, passed: newHealth >= 0.50, total_passed: allResults.filter(r => r.passed).length };
}

// ─────────────────────────────── Component ────────────────────────────────────
export default function ResultsPage() {
  return (
    <Suspense fallback={
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <span className="material-symbols-outlined text-4xl text-primary animate-spin">progress_activity</span>
        <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">Loading results…</p>
      </div>
    }>
      <ResultsContent />
    </Suspense>
  );
}

function ResultsContent() {
  const params = useParams();
  const router = useRouter();
  const searchParams = useSearchParams();
  const projectId = params.id as string;

  const [results, setResults] = useState<FullResults | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [activeTab, setActiveTab] = useState(0);
  const [tokenData, setTokenData] = useState<TokenSummary | null>(null);

  useEffect(() => {
    const runId =
      searchParams.get("run") || sessionStorage.getItem(`run_id_${projectId}`);
    if (!runId) {
      setError("No run ID found. Please run the test suite first.");
      setLoading(false);
      return;
    }

    authFetch(`${API}/api/job/${runId}/results`)
      .then((r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then((data) => {
        const base = { ...data, run_id: runId } as FullResults;
        // Re-apply any manually passed tests saved from a previous visit
        type SavedEntry = { phase: "functional" | "security" | "quality" | "rag"; originalScore: number; originalPassed: boolean };
        const saved = JSON.parse(sessionStorage.getItem(`manual_passes_${runId}`) || "{}") as Record<string, SavedEntry>;
        const withPasses = Object.entries(saved).reduce(
          (acc, [testId, entry]) => {
            const phaseData = acc[entry.phase];
            if (!phaseData) return acc;
            const preSeeded: FullResults = {
              ...acc,
              [entry.phase]: {
                ...phaseData,
                results: phaseData.results.map(r =>
                  r.test_id === testId
                    ? { ...r, original_score: entry.originalScore, original_passed: entry.originalPassed }
                    : r
                ),
              },
            };
            return applyManualPass(preSeeded, entry.phase, testId);
          },
          base,
        );
        setResults(withPasses);
        setLoading(false);
        // Fetch token summary from MLflow (best-effort, silent on failure)
        authFetch(`${API}/api/job/${runId}/token-summary`)
          .then(r => r.ok ? r.json() : null)
          .then(data => { if (data?.total_tokens > 0) setTokenData(data as TokenSummary); })
          .catch(() => {});
      })
      .catch((e) => {
        setError(e.message);
        setLoading(false);
      });
  }, [projectId]);

  const handleManualPass = useCallback((phase: "functional" | "security" | "quality" | "rag", testId: string) => {
    setResults(prev => {
      if (!prev) return prev;
      const key = `manual_passes_${prev.run_id}`;
      const saved = JSON.parse(sessionStorage.getItem(key) || "{}");
      const test = prev[phase]?.results.find(r => r.test_id === testId);
      if (test && !test.manually_passed) {
        saved[testId] = { phase, originalScore: test.score, originalPassed: test.passed };
        sessionStorage.setItem(key, JSON.stringify(saved));
      }
      const next = applyManualPass(prev, phase, testId);
      sessionStorage.setItem(`run_health_${prev.run_id}`, String(next.health_score));
      return next;
    });
  }, []);

  const handleManualRevert = useCallback((phase: "functional" | "security" | "quality" | "rag", testId: string) => {
    setResults(prev => {
      if (!prev) return prev;
      const key = `manual_passes_${prev.run_id}`;
      const saved = JSON.parse(sessionStorage.getItem(key) || "{}");
      delete saved[testId];
      sessionStorage.setItem(key, JSON.stringify(saved));
      const next = applyManualRevert(prev, phase, testId);
      sessionStorage.setItem(`run_health_${prev.run_id}`, String(next.health_score));
      return next;
    });
  }, []);

  const downloadJSON = () => {
    if (!results) return;
    const blob = new Blob([JSON.stringify(results, null, 2)], { type: "application/json" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metron_results_${Date.now()}.json`;
    a.click();
  };

  const downloadAllPromptsCSV = () => {
    if (!results) return;

    const escape = (s: string) => `"${String(s ?? "").replace(/"/g, '""')}"`;

    const headers = [
      "Suite", "Metric / Test", "Category",
      "Prompt (Input)", "AI Response (Output)",
      "Score", "Passed", "Reasoning", "Latency (ms)",
      "Taxonomy ID", "Taxonomy Label", "Failure Reason",
    ];

    const toRows = (suite: string, items: TestResult[] = []) =>
      items.map((r) => [
        escape(suite),
        escape(r.test_name || ""),
        escape(r.category || ""),
        escape((r.input_text || "").slice(0, 300)),
        escape((r.output_text || "").slice(0, 300)),
        r.score?.toFixed(4) ?? "",
        r.passed ? "PASS" : "FAIL",
        escape(r.reasoning || ""),
        r.latency_ms?.toFixed(0) ?? "",
        escape(r.failure_taxonomy_id || ""),
        escape(r.failure_taxonomy_label || ""),
        escape(r.failure_reason || ""),
      ]);

    const rows = [
      ...toRows("Functional", results.functional?.results),
      ...toRows("Security",   results.security?.results),
      ...toRows("Quality",    results.quality?.results),
    ];

    const csv = [headers, ...rows.map((r) => r.join(","))].join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metron_all_prompts_${Date.now()}.csv`;
    a.click();
  };

  const downloadCSV = () => {
    if (!results?.functional?.results) return;
    const headers = ["Test ID", "Name", "Category", "Score", "Passed", "Latency(ms)", "Input", "Output", "Taxonomy ID", "Taxonomy Label", "Failure Reason"];
    const escape = (s: string) => `"${String(s ?? "").replace(/"/g, '""')}"`;
    const rows = results.functional.results.map((r) => [
      r.test_id, r.test_name, r.category,
      r.score.toFixed(3), r.passed ? "true" : "false",
      r.latency_ms.toFixed(0),
      escape(r.input_text.slice(0, 100)),
      escape(r.output_text.slice(0, 200)),
      escape(r.failure_taxonomy_id || ""),
      escape(r.failure_taxonomy_label || ""),
      escape(r.failure_reason || ""),
    ]);
    const csv = [headers, ...rows].map((r) => r.join(",")).join("\n");
    const blob = new Blob([csv], { type: "text/csv" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metron_functional_${Date.now()}.csv`;
    a.click();
  };

  const downloadHTML = () => {
    if (!results?.report_html) return;
    const blob = new Blob([results.report_html], { type: "text/html" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metron_report_${Date.now()}.html`;
    a.click();
  };

  const downloadMarkdown = () => {
    if (!results) return;
    const r = results;


    const isFullRunMd = ["all", "tenant_admin", "super_admin"].includes(r.user_role ?? "all");
    const scoreLineMd = isFullRunMd
      ? `Health Score: ${(r.health_score * 100).toFixed(1)}% | ${r.passed ? "PASSED" : "FAILED"}`
      : `Tests Passed: ${r.total_passed}/${r.total_tests} | ${(r.user_role ?? "").replace(/_/g, " ")} run`;

    const summaryRows = [
      (r.functional?.total ?? 0) > 0 ? `| Functional | ${r.functional!.passed} | ${r.functional!.total} | ${r.functional!.pass_rate}% |` : null,
      (r.security?.total ?? 0) > 0   ? `| Security   | ${r.security!.passed}   | ${r.security!.total}   | ${r.security!.pass_rate}% |`   : null,
      (r.quality?.total ?? 0) > 0    ? `| Quality    | ${r.quality!.passed}    | ${r.quality!.total}    | ${r.quality!.pass_rate}% |`    : null,
    ].filter(Boolean).join("\n");

    const perfSection = (r.performance?.avg_latency ?? 0) > 0 ? `
## Performance
- Avg Latency: ${(r.performance!.avg_latency ?? 0).toFixed(0)}ms
- P95: ${(r.performance!.p95_latency ?? 0).toFixed(0)}ms
- Throughput: ${(r.performance!.throughput ?? 0).toFixed(2)} req/s
- Error Rate: ${(r.performance!.error_rate ?? 0).toFixed(1)}%` : "";

    const loadSection = (r.load?.requests_per_second ?? 0) > 0 ? `
## Load Test
- Concurrent Users: ${r.load!.concurrent_users ?? 0}
- Throughput: ${(r.load!.requests_per_second ?? 0).toFixed(2)} req/s
- Error Rate: ${(r.load!.error_rate ?? 0).toFixed(1)}%` : "";

    const md = `# METRON QA Report
Generated: ${new Date().toLocaleString()}
Agent: ${r.agent_name || "—"} | Domain: ${r.domain}
${scoreLineMd}

## Summary
| Phase | Passed | Total | Pass Rate |
|-------|--------|-------|-----------|
${summaryRows}
${perfSection}
${loadSection}`;
    const blob = new Blob([md], { type: "text/markdown" });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(blob);
    a.download = `metron_report_${Date.now()}.md`;
    a.click();
  };

  if (loading) {
    return (
      <div className="flex flex-col items-center justify-center h-64 gap-4">
        <span className="material-symbols-outlined text-4xl text-primary animate-spin">progress_activity</span>
        <p className="text-sm text-[var(--color-on-surface-variant)] opacity-60">Loading results…</p>
      </div>
    );
  }

  if (error || !results) {
    return (
      <div className="max-w-4xl mx-auto py-20 text-center space-y-4">
        <span className="material-symbols-outlined text-5xl text-error">error</span>
        <p className="text-lg font-semibold">{error || "Results not found"}</p>
        <button onClick={() => router.push(`/dashboard/project/${projectId}/configure`)} className="px-6 py-3 rounded-xl btn-primary text-sm">
          Start New Test
        </button>
      </div>
    );
  }

  const healthPct = Math.round(results.health_score * 100);
  const healthColor = healthPct >= 70 ? "text-secondary" : healthPct >= 40 ? "text-[#855300]" : "text-error";
  const isFullRun = ["all", "tenant_admin", "super_admin"].includes(results.user_role ?? "all");

  const ROLE_PHASES: Record<string, Set<string>> = {
    "functional_tester":   new Set(["functional"]),
    "security_tester":     new Set(["security"]),
    "quality":             new Set(["quality"]),
    "performance":         new Set(["performance"]),
    "load":                new Set(["load"]),
    "security+functional": new Set(["security", "functional"]),
    "functional+quality":  new Set(["functional", "quality"]),
    "performance+load":    new Set(["performance", "load"]),
    "all":                 new Set(["functional", "security", "quality", "performance", "load"]),
    "tenant_admin":        new Set(["functional", "security", "quality", "performance", "load"]),
    "super_admin":         new Set(["functional", "security", "quality", "performance", "load"]),
  };
  const activePhases = ROLE_PHASES[results.user_role ?? "all"] ?? new Set(["functional", "security", "quality", "performance", "load"]);

  const TABS = [
    ...(activePhases.has("functional") ? ["Functional"] : []),
    ...(activePhases.has("security") ? ["Security"] : []),
    ...(activePhases.has("quality") ? ["Quality"] : []),
    ...(results.rag && activePhases.has("functional") ? ["RAG"] : []),
    ...(activePhases.has("performance") ? ["Performance"] : []),
    ...(activePhases.has("load") ? ["Load Test"] : []),
    "LLMOps",
    "Export",
  ];

  return (
    <div className="max-w-5xl mx-auto pb-20 space-y-8 animate-fade-in">
      {/* Header */}
      <div className="flex flex-col md:flex-row gap-6 items-start justify-between">
        <div className="space-y-1.5">
          <div className="flex items-center gap-2">
            <span className="w-1.5 h-1.5 rounded-full bg-primary" />
            <span className="text-[10px] font-black uppercase tracking-[0.2em] text-primary">Results</span>
          </div>
          <h1 className="font-headline text-4xl font-black text-[var(--color-on-surface)] tracking-tighter">
            {results.agent_name || "Test Results"}
          </h1>
          <p className="text-[var(--color-on-surface-variant)] text-sm opacity-60">{results.domain}</p>
        </div>

        {/* Score card — health score for full runs, passed/total for partial */}
        <div className="card p-6 flex flex-col items-center gap-2 min-w-[140px]">
          {isFullRun ? (
            <>
              <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Health Score</p>
              <p className={`font-headline text-5xl font-black ${healthColor}`}>{healthPct}%</p>
              <span className={`text-xs font-bold px-3 py-1 rounded-full ${results.passed ? "badge-pass" : "badge-fail"}`}>
                {results.passed ? "PASSED" : "FAILED"}
              </span>
            </>
          ) : (
            <>
              <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Tests Passed</p>
              <p className="font-headline text-5xl font-black text-primary">{results.total_passed}/{results.total_tests}</p>
              <span className="text-xs font-bold text-[var(--color-on-surface-variant)] opacity-60 capitalize">
                {(results.user_role ?? "").replace(/_/g, " ")}
              </span>
            </>
          )}
        </div>
      </div>

      {/* Summary Cards — only show phases that were run */}
      <div className="grid grid-cols-2 md:grid-cols-5 gap-4">
        {[
          { label: "Functional", phase: "functional", icon: "science", color: "text-primary", value: results.functional ? `${results.functional.passed}/${results.functional.total}` : "—", sub: results.functional ? `${results.functional.pass_rate}%` : "" },
          { label: "Security", phase: "security", icon: "security", color: "text-error", value: results.security ? `${results.security.passed}/${results.security.total}` : "—", sub: results.security ? `${results.security.pass_rate}%` : "" },
          { label: "Quality", phase: "quality", icon: "grade", color: "text-secondary", value: results.quality ? `${results.quality.passed}/${results.quality.total}` : "—", sub: results.quality ? `${results.quality.pass_rate}%` : "" },
          { label: "Performance", phase: "performance", icon: "speed", color: "text-[#855300]", value: results.performance ? `${(results.performance.avg_latency ?? 0).toFixed(0)}ms` : "—", sub: "avg latency" },
          { label: "Load", phase: "load", icon: "group", color: "text-[var(--color-on-surface-variant)]", value: results.load ? `${(results.load.error_rate ?? 0).toFixed(1)}%` : "—", sub: "error rate" },
        ].filter(item => activePhases.has(item.phase)).map((item) => (
          <div key={item.label} className="card p-4 text-center">
            <span className={`material-symbols-outlined text-xl ${item.color}`}>{item.icon}</span>
            <p className="font-headline text-2xl font-black text-[var(--color-on-surface)] mt-1">{item.value}</p>
            <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">{item.sub || item.label}</p>
          </div>
        ))}
      </div>

      {/* Tabs */}
      <div className="card overflow-hidden">
        {/* Tab bar */}
        <div className="flex overflow-x-auto border-b border-[var(--color-outline-variant)]">
          {TABS.map((tab, i) => (
            <button
              key={tab}
              onClick={() => setActiveTab(i)}
              className={`tab-btn flex-shrink-0 ${activeTab === i ? "active" : ""}`}
            >
              {tab}
            </button>
          ))}
        </div>

        {/* Tab content */}
        <div className="p-6">
          {TABS[activeTab] === "Functional" && <FunctionalTab data={results.functional} personaBreakdown={results.persona_breakdown} onManualPass={(id) => handleManualPass("functional", id)} onManualRevert={(id) => handleManualRevert("functional", id)} />}
          {TABS[activeTab] === "Security" && <SecurityTab data={results.security} onManualPass={(id) => handleManualPass("security", id)} onManualRevert={(id) => handleManualRevert("security", id)} />}
          {TABS[activeTab] === "Quality" && <QualityTab data={results.quality} onManualPass={(id) => handleManualPass("quality", id)} onManualRevert={(id) => handleManualRevert("quality", id)} />}
          {TABS[activeTab] === "RAG" && <RAGTab data={results.rag} onManualPass={(id) => handleManualPass("rag", id)} onManualRevert={(id) => handleManualRevert("rag", id)} />}
          {TABS[activeTab] === "Performance" && <PerformanceTab data={results.performance} />}
          {TABS[activeTab] === "Load Test" && <LoadTab data={results.load} />}
          {TABS[activeTab] === "LLMOps" && <LLMOpsTab data={tokenData} />}
          {TABS[activeTab] === "Export" && (
            <div className="space-y-4">
              <p className="text-sm text-[var(--color-on-surface-variant)] opacity-70">Download the test results in various formats.</p>
              <div className="grid grid-cols-1 md:grid-cols-3 gap-4">
                <ExportCard
                  icon="data_object"
                  title="Full JSON Report"
                  description="Complete results with all data, suitable for programmatic processing."
                  onClick={downloadJSON}
                />
                <ExportCard
                  icon="checklist"
                  title="All Prompts CSV"
                  description="Every prompt across functional, security, and quality — with pass/fail, score, AI response, and reasoning."
                  onClick={downloadAllPromptsCSV}
                />
                <ExportCard
                  icon="table_chart"
                  title="Functional CSV"
                  description="Functional test results only as a spreadsheet-compatible CSV file."
                  onClick={downloadCSV}
                />
                {results.report_html && (
                  <ExportCard
                    icon="html"
                    title="HTML Report"
                    description="Styled full report with health score, persona breakdown, and failure drill-down."
                    onClick={downloadHTML}
                  />
                )}
                <ExportCard
                  icon="description"
                  title="Markdown Report"
                  description="Human-readable summary report in Markdown format."
                  onClick={downloadMarkdown}
                />
              </div>
            </div>
          )}
        </div>
      </div>

      {/* CTA */}
      <div className="flex justify-center">
        <button
          onClick={() => router.push(`/dashboard/project/${projectId}/configure`)}
          className="flex items-center gap-2 px-6 py-3 rounded-xl border border-[var(--color-outline)] text-sm font-semibold hover:bg-[var(--color-surface-variant)] transition-colors"
        >
          <span className="material-symbols-outlined text-base">refresh</span>
          Run New Test
        </button>
      </div>
    </div>
  );
}

// ─────────────────────── Functional Tab ────────────────────────────────────
function FunctionalTab({
  data,
  personaBreakdown,
  onManualPass,
  onManualRevert,
}: {
  data: PhaseSummary;
  personaBreakdown: FullResults["persona_breakdown"];
  onManualPass?: (id: string) => void;
  onManualRevert?: (id: string) => void;
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (!data?.results) return <EmptyState />;

  // Group by category
  const byCategory: Record<string, TestResult[]> = {};
  for (const r of data.results) {
    const cat = r.category || "other";
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(r);
  }

  const toggle = (id: string) => setExpanded((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="space-y-6">
      <SummaryRow4
        items={[
          { label: "Total Tests", value: data.total },
          { label: "Passed", value: data.passed, color: "text-secondary" },
          { label: "Failed", value: data.failed, color: "text-error" },
          { label: "Skipped", value: data.skipped ?? 0, color: (data.skipped ?? 0) > 0 ? "text-[#855300]" : undefined },
        ]}
      />

      {(data.evaluation_warnings?.length ?? 0) > 0 && (
        <div className="space-y-2">
          {data.evaluation_warnings!.map((w, i) => (
            <div key={i} className="flex items-start gap-2 p-3 rounded-xl border border-[#855300]/30 bg-[#855300]/5">
              <span className="material-symbols-outlined text-base text-[#855300] flex-shrink-0 mt-0.5">warning</span>
              <p className="text-xs text-[#855300] leading-relaxed">{w}</p>
            </div>
          ))}
        </div>
      )}

      {Object.entries(byCategory).map(([cat, results]) => {
        const catPassed = results.filter((r) => r.passed).length;
        const catRate = Math.round((catPassed / results.length) * 100);
        return (
          <div key={cat} className="border border-[var(--color-outline-variant)] rounded-xl overflow-hidden">
            <button
              onClick={() => toggle(cat)}
              className="w-full flex items-center justify-between p-4 hover:bg-[var(--color-surface-container-low)] transition-colors"
            >
              <div className="flex items-center gap-3">
                <span className="material-symbols-outlined text-base text-primary">category</span>
                <p className="text-sm font-black">{metricLabel(cat)}</p>
              </div>
              <div className="flex items-center gap-3">
                <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${catRate >= 70 ? "badge-pass" : "badge-fail"}`}>
                  {catPassed}/{results.length} ({catRate}%)
                </span>
                <span className="material-symbols-outlined text-base text-[var(--color-on-surface-variant)]">
                  {expanded.has(cat) ? "expand_less" : "expand_more"}
                </span>
              </div>
            </button>
            {expanded.has(cat) && (
              <div className="divide-y divide-[var(--color-outline-variant)]">
                {results.map((r) => (
                  <TestResultRow key={r.test_id} result={r} onManualPass={onManualPass} onManualRevert={onManualRevert} />
                ))}
              </div>
            )}
          </div>
        );
      })}

      {personaBreakdown?.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Persona Breakdown</p>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-3">
            {personaBreakdown.map((p) => (
              <div key={p.persona_id} className="p-3 rounded-xl border border-[var(--color-outline-variant)] flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-black truncate">{p.persona_name}</p>
                  <div className="flex flex-wrap gap-1 mt-1">
                    {(p.intent || p.fishbone?.intent) && (
                      <span className={`text-[10px] px-1.5 py-0.5 rounded-full font-semibold ${
                        (p.intent || p.fishbone?.intent) === "adversarial" ? "bg-error/10 text-error" :
                        (p.intent || p.fishbone?.intent) === "edge_case" ? "bg-[#855300]/10 text-[#855300]" :
                        "bg-primary/10 text-primary"
                      }`}>{p.intent || p.fishbone?.intent}</span>
                    )}
                    {p.fishbone?.expertise && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--color-surface-container-low)] border border-[var(--color-outline-variant)] font-semibold">
                        {p.fishbone.expertise}
                      </span>
                    )}
                    {p.fishbone?.emotional_state && (
                      <span className="text-[10px] px-1.5 py-0.5 rounded-full bg-[var(--color-surface-container-low)] border border-[var(--color-outline-variant)] font-semibold">
                        {p.fishbone.emotional_state}
                      </span>
                    )}
                    {!p.intent && !p.fishbone && (
                      <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">{p.total} tests</p>
                    )}
                  </div>
                </div>
                <div className="text-right">
                  <p className={`text-sm font-black ${p.pass_rate >= 70 ? "text-secondary" : "text-error"}`}>{p.pass_rate}%</p>
                  <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">{p.passed}/{p.total} passed</p>
                </div>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}

// ─────────────────────── Security Tab ─────────────────────────────────────
function SecurityTab({ data, onManualPass, onManualRevert }: { data: PhaseSummary; onManualPass?: (id: string) => void; onManualRevert?: (id: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (!data?.results) return <EmptyState />;

  const secScore = data.pass_rate;
  const scoreColor = secScore >= 90 ? "text-secondary" : secScore >= 70 ? "text-[#855300]" : "text-error";
  const toggle = (id: string) => setExpanded((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  // Group by full metric name (category field now holds the full metric_name)
  const byCategory: Record<string, TestResult[]> = {};
  for (const r of data.results) {
    const cat = r.category || "general";
    if (!byCategory[cat]) byCategory[cat] = [];
    byCategory[cat].push(r);
  }

  // Ordered display: detection metrics first, then resistance metrics
  const ORDER = ["pii_leakage", "toxicity", "prompt_injection", "bias_fairness", "toxic_request", "attack_resistance"];
  const sortedEntries = Object.entries(byCategory).sort(([a], [b]) => {
    const ai = ORDER.indexOf(a); const bi = ORDER.indexOf(b);
    return (ai === -1 ? 99 : ai) - (bi === -1 ? 99 : bi);
  });

  return (
    <div className="space-y-6">
      <div className="flex items-center gap-4 p-4 rounded-xl bg-[var(--color-surface-container-low)]">
        <span className={`font-headline text-4xl font-black ${scoreColor}`}>{secScore.toFixed(1)}%</span>
        <div>
          <p className="font-semibold">Security Score</p>
          <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">
            {data.passed}/{data.total} checks passed
            {(data.skipped ?? 0) > 0 && ` · ${data.skipped} skipped`}
            {secScore >= 90 ? " — Excellent" : secScore >= 70 ? " — Acceptable" : " — Needs Improvement"}
          </p>
        </div>
      </div>

      {(data.evaluation_warnings?.length ?? 0) > 0 && (
        <div className="space-y-2">
          {data.evaluation_warnings!.map((w, i) => (
            <div key={i} className="flex items-start gap-2 p-3 rounded-xl border border-[#855300]/30 bg-[#855300]/5">
              <span className="material-symbols-outlined text-base text-[#855300] flex-shrink-0 mt-0.5">warning</span>
              <p className="text-xs text-[#855300] leading-relaxed">{w}</p>
            </div>
          ))}
        </div>
      )}

      {sortedEntries.map(([cat, results]) => {
        const passed = results.filter((r) => r.passed).length;
        const passLabel = isDetectionMetric(cat)
          ? `${passed}/${results.length} clean`
          : `${passed}/${results.length} blocked`;
        const inputLabel = isResistanceMetric(cat) ? "Attack Prompt" : "Input";
        const outputLabel = "AI Response";

        return (
          <div key={cat} className="border border-[var(--color-outline-variant)] rounded-xl overflow-hidden">
            <button
              onClick={() => toggle(cat)}
              className="w-full flex items-center justify-between p-4 hover:bg-[var(--color-surface-container-low)] transition-colors"
            >
              <div className="flex items-center gap-3">
                <span className={`material-symbols-outlined text-base ${passed === results.length ? "text-secondary" : "text-error"}`}>
                  {passed === results.length ? "shield" : "gpp_bad"}
                </span>
                <p className="text-sm font-black">{metricLabel(cat)}</p>
              </div>
              <div className="flex items-center gap-3">
                <span className={`text-xs font-bold px-2 py-0.5 rounded-full ${passed === results.length ? "badge-pass" : "badge-fail"}`}>
                  {passLabel}
                </span>
                <span className="material-symbols-outlined text-base text-[var(--color-on-surface-variant)]">
                  {expanded.has(cat) ? "expand_less" : "expand_more"}
                </span>
              </div>
            </button>
            {expanded.has(cat) && (
              <div className="divide-y divide-[var(--color-outline-variant)]">
                {results.map((r) => (
                  <div key={r.test_id} className="p-4 space-y-2">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className={`material-symbols-outlined text-base ${r.passed ? "text-secondary" : "text-error"}`}>
                        {r.passed ? "check_circle" : "cancel"}
                      </span>
                      <p className="text-sm font-semibold">{r.test_name}</p>
                      <span className={`text-[10px] font-black ml-auto ${r.passed ? "text-secondary" : "text-error"}`}>
                        {(r.score * 100).toFixed(0)}%
                      </span>
                      {(onManualPass || onManualRevert) && (
                        <button
                          onClick={() => { r.manually_passed ? onManualRevert?.(r.test_id) : onManualPass?.(r.test_id); }}
                          className={`text-[10px] px-2 py-0.5 rounded-full font-bold border transition-colors ${
                            r.manually_passed
                              ? "bg-amber-500/10 text-amber-600 border-amber-500/30 hover:bg-amber-500/20"
                              : "border-[var(--color-outline)] text-[var(--color-on-surface-variant)] hover:bg-secondary/10 hover:text-secondary hover:border-secondary/30"
                          }`}
                        >
                          {r.manually_passed ? "Revert" : "Pass"}
                        </button>
                      )}
                    </div>
                    {r.reasoning && (
                      <p className="text-xs text-[var(--color-on-surface-variant)] pl-8 leading-relaxed">{r.reasoning}</p>
                    )}
                    {!r.passed && r.failure_reason && (
                      <div className="p-3 rounded-lg border border-[#6200ee]/20 bg-[#6200ee]/5 space-y-1.5">
                        <div className="flex items-center gap-2 flex-wrap">
                          <span className="material-symbols-outlined text-xs text-[#6200ee]">travel_explore</span>
                          <p className="text-[10px] font-black uppercase tracking-wider text-[#6200ee]">Root Cause</p>
                          {r.failure_taxonomy_id && r.failure_taxonomy_label && (
                            <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-[#ede7f6] text-[#6200ee]">
                              {r.failure_taxonomy_id} · {r.failure_taxonomy_label}
                            </span>
                          )}
                        </div>
                        <p className="text-xs text-[var(--color-on-surface-variant)] leading-relaxed">{r.failure_reason}</p>
                      </div>
                    )}
                    <ConversationBlock
                      input={r.input_text}
                      output={r.output_text}
                      inputLabel={inputLabel}
                      outputLabel={outputLabel}
                    />
                  </div>
                ))}
              </div>
            )}
          </div>
        );
      })}
    </div>
  );
}

// ─────────────────────── Quality Tab ──────────────────────────────────────
function QualityTab({ data, onManualPass, onManualRevert }: { data: PhaseSummary; onManualPass?: (id: string) => void; onManualRevert?: (id: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (!data?.results) return <EmptyState />;

  const toggle = (id: string) => setExpanded((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  return (
    <div className="space-y-6">
      <SummaryRow4
        items={[
          { label: "Total", value: data.total },
          { label: "Passed", value: data.passed, color: "text-secondary" },
          { label: "Failed", value: data.failed, color: "text-error" },
          { label: "Skipped", value: data.skipped ?? 0, color: (data.skipped ?? 0) > 0 ? "text-[#855300]" : undefined },
        ]}
      />

      {(data.evaluation_warnings?.length ?? 0) > 0 && (
        <div className="space-y-2">
          {data.evaluation_warnings!.map((w, i) => (
            <div key={i} className="flex items-start gap-2 p-3 rounded-xl border border-[#855300]/30 bg-[#855300]/5">
              <span className="material-symbols-outlined text-base text-[#855300] flex-shrink-0 mt-0.5">warning</span>
              <p className="text-xs text-[#855300] leading-relaxed">{w}</p>
            </div>
          ))}
        </div>
      )}

      {data.results.map((r) => (
        <div key={r.test_id} className="border border-[var(--color-outline-variant)] rounded-xl overflow-hidden">
          <div
            onClick={() => toggle(r.test_id)}
            className="w-full flex items-center justify-between p-4 hover:bg-[var(--color-surface-container-low)] transition-colors cursor-pointer"
          >
            <div className="flex items-center gap-3">
              <span className={`material-symbols-outlined text-base ${r.passed ? "text-secondary" : "text-error"}`}>
                {r.passed ? "check_circle" : "cancel"}
              </span>
              <p className="text-sm font-black">{r.test_name}</p>
            </div>
            <div className="flex items-center gap-2">
              <span className={`text-xs font-black ${r.passed ? "text-secondary" : "text-error"}`}>
                {(r.score * 100).toFixed(0)}%
              </span>
              {(onManualPass || onManualRevert) && (
                <button
                  onClick={e => { e.stopPropagation(); r.manually_passed ? onManualRevert?.(r.test_id) : onManualPass?.(r.test_id); }}
                  className={`text-[10px] px-2 py-0.5 rounded-full font-bold border transition-colors ${
                    r.manually_passed
                      ? "bg-amber-500/10 text-amber-600 border-amber-500/30 hover:bg-amber-500/20"
                      : "border-[var(--color-outline)] text-[var(--color-on-surface-variant)] hover:bg-secondary/10 hover:text-secondary hover:border-secondary/30"
                  }`}
                >
                  {r.manually_passed ? "Revert" : "Pass"}
                </button>
              )}
              <span className="material-symbols-outlined text-base text-[var(--color-on-surface-variant)]">
                {expanded.has(r.test_id) ? "expand_less" : "expand_more"}
              </span>
            </div>
          </div>
          {expanded.has(r.test_id) && (
            <div className="px-4 pb-4 space-y-3 border-t border-[var(--color-outline-variant)]">
              <ConversationBlock input={r.input_text} output={r.output_text} inputLabel="Question" outputLabel="Response" />
              {r.reasoning && (
                <div className="p-3 rounded-lg bg-[var(--color-surface-container-low)]">
                  <p className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] opacity-60 mb-1">Evaluation</p>
                  <p className="text-xs">{r.reasoning}</p>
                </div>
              )}
              {!r.passed && r.failure_reason && (
                <div className="p-3 rounded-lg border border-[#6200ee]/20 bg-[#6200ee]/5 space-y-1.5">
                  <div className="flex items-center gap-2 flex-wrap">
                    <span className="material-symbols-outlined text-xs text-[#6200ee]">travel_explore</span>
                    <p className="text-[10px] font-black uppercase tracking-wider text-[#6200ee]">Root Cause</p>
                    {r.failure_taxonomy_id && r.failure_taxonomy_label && (
                      <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-[#ede7f6] text-[#6200ee]">
                        {r.failure_taxonomy_id} · {r.failure_taxonomy_label}
                      </span>
                    )}
                  </div>
                  <p className="text-xs text-[var(--color-on-surface-variant)] leading-relaxed">{r.failure_reason}</p>
                </div>
              )}
              {/* Metric breakdown from details */}
              {(() => {
                type MetricEntry = { metric_name: string; score: number; passed: boolean };
                const metrics = (r.details as { metrics?: MetricEntry[] })?.metrics;
                if (!Array.isArray(metrics) || metrics.length === 0) return null;
                return (
                  <div className="space-y-2">
                    {metrics.map((m, i) => (
                      <div key={i} className="flex items-center gap-3 text-xs">
                        <span className={`material-symbols-outlined text-sm ${m.passed ? "text-secondary" : "text-error"}`}>
                          {m.passed ? "check" : "close"}
                        </span>
                        <span className="font-semibold capitalize">{m.metric_name.replace(/_/g, " ")}</span>
                        <span className="ml-auto font-black">{(m.score * 100).toFixed(0)}%</span>
                      </div>
                    ))}
                  </div>
                );
              })()}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}

// ─────────────────────── RAG Tab ──────────────────────────────────────────
function RAGTab({ data, onManualPass, onManualRevert }: { data: PhaseSummary; onManualPass?: (id: string) => void; onManualRevert?: (id: string) => void }) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  if (!data?.results) return <EmptyState />;

  const toggle = (id: string) => setExpanded((prev) => { const n = new Set(prev); n.has(id) ? n.delete(id) : n.add(id); return n; });

  const RAG_METRIC_ICONS: Record<string, string> = {
    rag_faithfulness:      "verified",
    rag_context_relevancy: "manage_search",
    rag_context_recall:    "library_books",
    rag_context_precision: "target",
  };

  const byMetric = data.results.reduce<Record<string, TestResult[]>>((acc, r) => {
    (acc[r.category] = acc[r.category] || []).push(r);
    return acc;
  }, {});

  return (
    <div className="space-y-6">
      <SummaryRow4
        items={[
          { label: "Total", value: data.total },
          { label: "Passed", value: data.passed, color: "text-secondary" },
          { label: "Failed", value: data.failed, color: "text-error" },
          { label: "Avg Score", value: `${(data.avg_score * 100).toFixed(1)}%` },
        ]}
      />

      {/* Per-metric summary cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {Object.entries(byMetric).map(([metricKey, items]) => {
          const avg = items.reduce((s, r) => s + r.score, 0) / items.length;
          const passed = items.filter((r) => r.passed).length;
          return (
            <div key={metricKey} className="card p-4 text-center space-y-1">
              <span className="material-symbols-outlined text-xl text-primary">
                {RAG_METRIC_ICONS[metricKey] || "analytics"}
              </span>
              <p className="font-headline text-2xl font-black text-[var(--color-on-surface)]">{(avg * 100).toFixed(0)}%</p>
              <p className="text-[9px] font-black uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">
                {metricLabel(metricKey)}
              </p>
              <p className="text-[10px] text-secondary font-bold">{passed}/{items.length} passed</p>
            </div>
          );
        })}
      </div>

      {/* Per-question breakdown */}
      <div className="space-y-3">
        {data.results.map((r) => (
          <div key={r.test_id} className="border border-[var(--color-outline-variant)] rounded-xl overflow-hidden">
            <div
              onClick={() => toggle(r.test_id)}
              className="w-full flex items-center justify-between p-4 hover:bg-[var(--color-surface-container-low)] transition-colors cursor-pointer"
            >
              <div className="flex items-center gap-3 min-w-0">
                <span className={`material-symbols-outlined text-base flex-shrink-0 ${r.passed ? "text-secondary" : "text-error"}`}>
                  {r.passed ? "check_circle" : "cancel"}
                </span>
                <div className="min-w-0">
                  <p className="text-xs font-black text-[var(--color-on-surface-variant)] opacity-60 uppercase tracking-wider">{metricLabel(r.category)}</p>
                  <p className="text-sm font-bold truncate">{r.input_text.slice(0, 80)}</p>
                </div>
              </div>
              <div className="flex items-center gap-2 flex-shrink-0">
                <span className={`text-xs font-black ${r.passed ? "text-secondary" : "text-error"}`}>{(r.score * 100).toFixed(0)}%</span>
                {(onManualPass || onManualRevert) && (
                  <button
                    onClick={e => { e.stopPropagation(); r.manually_passed ? onManualRevert?.(r.test_id) : onManualPass?.(r.test_id); }}
                    className={`text-[10px] px-2 py-0.5 rounded-full font-bold border transition-colors ${
                      r.manually_passed
                        ? "bg-amber-500/10 text-amber-600 border-amber-500/30 hover:bg-amber-500/20"
                        : "border-[var(--color-outline)] text-[var(--color-on-surface-variant)] hover:bg-secondary/10 hover:text-secondary hover:border-secondary/30"
                    }`}
                  >
                    {r.manually_passed ? "Revert" : "Pass"}
                  </button>
                )}
                <span className="material-symbols-outlined text-base text-[var(--color-on-surface-variant)]">
                  {expanded.has(r.test_id) ? "expand_less" : "expand_more"}
                </span>
              </div>
            </div>
            {expanded.has(r.test_id) && (
              <div className="px-4 pb-4 space-y-3 border-t border-[var(--color-outline-variant)]">
                <ConversationBlock input={r.input_text} output={r.output_text} inputLabel="Question" outputLabel="RAG Answer" />
                {r.reasoning && (
                  <div className="p-3 rounded-lg bg-[var(--color-surface-container-low)]">
                    <p className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] opacity-60 mb-1">Evaluation</p>
                    <p className="text-xs">{r.reasoning}</p>
                  </div>
                )}
                {!r.passed && r.failure_reason && (
                  <div className="p-3 rounded-lg border border-[#6200ee]/20 bg-[#6200ee]/5 space-y-1.5">
                    <div className="flex items-center gap-2 flex-wrap">
                      <span className="material-symbols-outlined text-xs text-[#6200ee]">travel_explore</span>
                      <p className="text-[10px] font-black uppercase tracking-wider text-[#6200ee]">Root Cause</p>
                      {r.failure_taxonomy_id && r.failure_taxonomy_label && (
                        <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-[#ede7f6] text-[#6200ee]">
                          {r.failure_taxonomy_id} · {r.failure_taxonomy_label}
                        </span>
                      )}
                    </div>
                    <p className="text-xs text-[var(--color-on-surface-variant)] leading-relaxed">{r.failure_reason}</p>
                  </div>
                )}
              </div>
            )}
          </div>
        ))}
      </div>
    </div>
  );
}


// ─────────────────────── Performance Tab ──────────────────────────────────
function PerformanceTab({ data }: { data: PerformanceMetrics }) {
  if (!data) return <EmptyState />;

  const metrics = [
    ["Avg Latency", `${(data.avg_latency ?? 0).toFixed(0)}ms`],
    ["P95 Latency", `${(data.p95_latency ?? 0).toFixed(0)}ms`],
    ["Throughput", `${(data.throughput ?? 0).toFixed(2)} req/s`],
    ["Error Rate", `${(data.error_rate ?? 0).toFixed(1)}%`],
    ["Min Latency", `${(data.min_latency ?? 0).toFixed(0)}ms`],
    ["Max Latency", `${(data.max_latency ?? 0).toFixed(0)}ms`],
    ["P50 (Median)", `${(data.median_latency ?? 0).toFixed(0)}ms`],
    ["P99 Latency", `${(data.p99_latency ?? 0).toFixed(0)}ms`],
  ];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {metrics.map(([label, value]) => (
          <MetricCard key={label} label={label} value={value} />
        ))}
      </div>

      <div className="p-4 rounded-xl bg-[var(--color-surface-container-low)]">
        <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60 mb-2">Details</p>
        <div className="grid grid-cols-2 gap-3 text-sm">
          <div className="flex justify-between">
            <span className="text-[var(--color-on-surface-variant)] opacity-70">Total Requests</span>
            <span className="font-semibold">{data.total_requests ?? 0}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--color-on-surface-variant)] opacity-70">Successful</span>
            <span className="font-semibold text-secondary">{data.successful ?? 0}</span>
          </div>
          <div className="flex justify-between">
            <span className="text-[var(--color-on-surface-variant)] opacity-70">Errors</span>
            <span className="font-semibold text-error">{data.errors ?? 0}</span>
          </div>
        </div>
      </div>
    </div>
  );
}

// ─────────────────────── Load Tab ─────────────────────────────────────────
function LoadTab({ data }: { data: LoadMetrics }) {
  if (!data) return <EmptyState />;

  const errRate = data.error_rate ?? 0;
  const errColor = errRate < 1 ? "text-secondary" : errRate < 5 ? "text-[#855300]" : "text-error";
  const assessment = errRate < 1 ? "Excellent — minimal errors" : errRate < 5 ? "Acceptable — some errors detected" : "Critical — high error rate";

  const metrics = [
    ["Concurrent Users", data.concurrent_users ?? 0],
    ["Duration", `${(data.duration_seconds ?? 0).toFixed(1)}s`],
    ["Total Requests", data.total_requests ?? 0],
    ["Successful", data.successful ?? 0],
    ["Failed", data.errors ?? 0],
    ["Avg Latency", `${(data.avg_latency ?? 0).toFixed(0)}ms`],
    ["P95 Latency", `${(data.p95_latency ?? 0).toFixed(0)}ms`],
    ["Throughput", `${(data.requests_per_second ?? 0).toFixed(2)} rps`],
  ];

  return (
    <div className="space-y-6">
      <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
        {metrics.map(([label, value]) => (
          <MetricCard key={label} label={String(label)} value={String(value)} />
        ))}
      </div>

      <div className={`p-4 rounded-xl border flex items-center gap-3 ${
        errRate < 1 ? "border-secondary/30 bg-secondary/5" :
        errRate < 5 ? "border-[#855300]/30 bg-[#855300]/5" :
        "border-error/30 bg-error/5"
      }`}>
        <span className={`material-symbols-outlined ${errColor}`}>
          {errRate < 1 ? "check_circle" : errRate < 5 ? "warning" : "error"}
        </span>
        <div>
          <p className={`text-sm font-black ${errColor}`}>Error Rate: {errRate.toFixed(1)}%</p>
          <p className="text-xs text-[var(--color-on-surface-variant)] opacity-70">{assessment}</p>
        </div>
      </div>

      {data.tool_used && (
        <p className="text-xs text-[var(--color-on-surface-variant)] opacity-50">Tool: {data.tool_used}</p>
      )}
    </div>
  );
}

// ─────────────────────── Shared mini-components ────────────────────────────
function TestResultRow({ result, onManualPass, onManualRevert }: { result: TestResult; onManualPass?: (id: string) => void; onManualRevert?: (id: string) => void }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-t border-[var(--color-outline-variant)] first:border-0">
      <div onClick={() => setOpen(!open)} className="w-full flex items-center justify-between p-3 hover:bg-[var(--color-surface-container-low)] transition-colors text-left gap-3 cursor-pointer">
        <div className="flex items-center gap-3 min-w-0">
          <span className={`material-symbols-outlined text-sm flex-shrink-0 ${result.passed ? "text-secondary" : "text-error"}`}>
            {result.passed ? "check_circle" : "cancel"}
          </span>
          <p className="text-xs font-semibold truncate">{result.test_name}</p>
        </div>
        <div className="flex items-center gap-2 flex-shrink-0">
          <span className="text-xs text-[var(--color-on-surface-variant)] opacity-60">{result.latency_ms.toFixed(0)}ms</span>
          <span className={`text-xs font-black ${result.passed ? "text-secondary" : "text-error"}`}>{(result.score * 100).toFixed(0)}%</span>
          {(onManualPass || onManualRevert) && (
            <button
              onClick={e => { e.stopPropagation(); result.manually_passed ? onManualRevert?.(result.test_id) : onManualPass?.(result.test_id); }}
              className={`text-[10px] px-2 py-0.5 rounded-full font-bold border transition-colors ${
                result.manually_passed
                  ? "bg-amber-500/10 text-amber-600 border-amber-500/30 hover:bg-amber-500/20"
                  : "border-[var(--color-outline)] text-[var(--color-on-surface-variant)] hover:bg-secondary/10 hover:text-secondary hover:border-secondary/30"
              }`}
            >
              {result.manually_passed ? "Revert" : "Pass"}
            </button>
          )}
          <span className="material-symbols-outlined text-sm text-[var(--color-on-surface-variant)]">{open ? "expand_less" : "expand_more"}</span>
        </div>
      </div>
      {open && (
        <div className="px-4 pb-3 space-y-2">
          <ConversationBlock input={result.input_text} output={result.output_text} />
          {result.reasoning && (
            <p className="text-xs text-[var(--color-on-surface-variant)] italic">{result.reasoning}</p>
          )}
          {!result.passed && result.failure_reason && (
            <div className="p-3 rounded-lg border border-[#6200ee]/20 bg-[#6200ee]/5 space-y-1.5">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="material-symbols-outlined text-xs text-[#6200ee]">travel_explore</span>
                <p className="text-[10px] font-black uppercase tracking-wider text-[#6200ee]">Root Cause</p>
                {result.failure_taxonomy_id && result.failure_taxonomy_label && (
                  <span className="text-[10px] font-bold px-2 py-0.5 rounded-full bg-[#ede7f6] text-[#6200ee]">
                    {result.failure_taxonomy_id} · {result.failure_taxonomy_label}
                  </span>
                )}
              </div>
              <p className="text-xs text-[var(--color-on-surface-variant)] leading-relaxed">{result.failure_reason}</p>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function ConversationBlock({
  input, output, inputLabel = "Input", outputLabel = "Response"
}: { input: string; output: string; inputLabel?: string; outputLabel?: string }) {
  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-2">
      <div className="p-3 rounded-lg bg-[var(--color-surface-container-low)]">
        <p className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] opacity-60 mb-1">{inputLabel}</p>
        <p className="text-xs font-mono break-all">{input || "—"}</p>
      </div>
      <div className="p-3 rounded-lg bg-[var(--color-surface-container-low)]">
        <p className="text-[10px] font-bold uppercase tracking-wider text-[var(--color-on-surface-variant)] opacity-60 mb-1">{outputLabel}</p>
        <p className="text-xs break-all">{output || "—"}</p>
      </div>
    </div>
  );
}

function SummaryRow4({ items }: { items: Array<{ label: string; value: number | string; color?: string }> }) {
  return (
    <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
      {items.map((item) => (
        <div key={item.label} className="p-4 rounded-xl bg-[var(--color-surface-container-low)] text-center">
          <p className={`font-headline text-2xl font-black ${item.color || "text-[var(--color-on-surface)]"}`}>{item.value}</p>
          <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 mt-0.5">{item.label}</p>
        </div>
      ))}
    </div>
  );
}

function MetricCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="p-4 rounded-xl bg-[var(--color-surface-container-low)] text-center">
      <p className="font-headline text-xl font-black text-[var(--color-on-surface)]">{value}</p>
      <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 mt-0.5">{label}</p>
    </div>
  );
}

function ExportCard({ icon, title, description, onClick }: { icon: string; title: string; description: string; onClick: () => void }) {
  return (
    <button
      onClick={onClick}
      className="card p-5 text-left space-y-3 hover:shadow-lg hover:shadow-primary/5 transition-shadow group"
    >
      <span className="material-symbols-outlined text-2xl text-primary group-hover:scale-110 transition-transform">{icon}</span>
      <div>
        <p className="font-headline text-sm font-black">{title}</p>
        <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 mt-1">{description}</p>
      </div>
      <div className="flex items-center gap-2 text-xs font-semibold text-primary">
        <span className="material-symbols-outlined text-sm">download</span>
        Download
      </div>
    </button>
  );
}

function EmptyState() {
  return (
    <div className="py-12 text-center space-y-2">
      <span className="material-symbols-outlined text-3xl text-[var(--color-on-surface-variant)] opacity-30">data_usage</span>
      <p className="text-sm text-[var(--color-on-surface-variant)] opacity-50">No data available for this phase.</p>
    </div>
  );
}

// ─────────────────────── LLMOps Tab ────────────────────────────────────────
const STAGE_LABELS: Record<string, string> = {
  s0: "S0 Profile",
  s1: "S1 Personas",
  s2: "S2 Test Gen",
  s3: "S3 Execution",
  s4: "S4 Eval",
  s5: "S5 Aggregate",
  s7: "S7 Report",
  s8: "S8 RCA",
  unknown: "Other",
};

function LLMOpsTab({ data }: { data: TokenSummary | null }) {
  const mlflowUrl = process.env.NEXT_PUBLIC_MLFLOW_URL;

  if (!data || data.total_tokens === 0) {
    return (
      <div className="py-16 text-center space-y-3">
        <span className="material-symbols-outlined text-4xl text-[var(--color-on-surface-variant)] opacity-30">monitoring</span>
        <p className="text-sm font-semibold text-[var(--color-on-surface-variant)] opacity-60">No LLM usage data available</p>
        <p className="text-xs text-[var(--color-on-surface-variant)] opacity-40">Set MLFLOW_TRACKING_URI on the backend to enable token tracking.</p>
      </div>
    );
  }

  const {
    total_calls, total_prompt_tokens, total_completion_tokens, total_tokens,
    avg_latency_ms, tpot_ms = 0, token_efficiency_ratio = 0, tpm_velocity = 0,
    retry_count = 0, truncated_calls = 0, truncation_rate = 0,
    models_used = {}, by_stage,
  } = data;

  const stageEntries = Object.entries(by_stage).sort((a, b) => b[1].total_tokens - a[1].total_tokens);
  const maxStageTokens = stageEntries[0]?.[1].total_tokens || 1;
  const promptPct = total_tokens > 0 ? Math.round((total_prompt_tokens / total_tokens) * 100) : 0;

  const modelEntries = Object.entries(models_used).sort((a, b) => b[1] - a[1]);

  return (
    <div className="space-y-8">

      {/* Row 1 — Token counts */}
      <div>
        <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60 mb-3">Token Counts</p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "LLM Calls",        value: total_calls.toLocaleString(),                    color: "text-primary" },
            { label: "Input Tokens",      value: total_prompt_tokens.toLocaleString(),            color: "text-[var(--color-on-surface)]" },
            { label: "Output Tokens",     value: total_completion_tokens.toLocaleString(),        color: "text-[var(--color-on-surface)]" },
            { label: "Total Tokens",      value: total_tokens.toLocaleString(),                   color: "text-secondary" },
          ].map(c => (
            <div key={c.label} className="p-4 rounded-xl bg-[var(--color-surface-container-low)] text-center">
              <p className={`font-headline text-2xl font-black ${c.color}`}>{c.value}</p>
              <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 mt-0.5">{c.label}</p>
            </div>
          ))}
        </div>
      </div>

      {/* Row 2 — Performance metrics */}
      <div>
        <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60 mb-3">Performance</p>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
          {[
            { label: "Avg Call Latency",   value: avg_latency_ms > 0 ? `${avg_latency_ms.toFixed(0)}ms` : "—",   color: "text-[#855300]",
              tip: "Average latency of METRON's internal LLM calls — not the target AI's response time" },
            { label: "TPOT",              value: tpot_ms > 0 ? `${tpot_ms.toFixed(1)}ms/tok` : "—",            color: "text-[#855300]",
              tip: "Time Per Output Token — total latency ÷ total output tokens" },
            { label: "TPM Velocity",      value: tpm_velocity > 0 ? `${tpm_velocity.toFixed(0)}/min` : "—",    color: "text-primary",
              tip: "Tokens processed per minute over the full pipeline run" },
            { label: "Token Efficiency",  value: token_efficiency_ratio > 0 ? token_efficiency_ratio.toFixed(3) : "—", color: "text-secondary",
              tip: "Output tokens ÷ Input tokens — higher = more output per prompt" },
          ].map(c => (
            <div key={c.label} className="p-4 rounded-xl bg-[var(--color-surface-container-low)] text-center" title={"tip" in c ? c.tip : undefined}>
              <p className={`font-headline text-2xl font-black ${c.color}`}>{c.value}</p>
              <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60 mt-0.5">{c.label}</p>
              {c.label === "Avg Call Latency" && (
                <p className="text-[10px] text-[var(--color-on-surface-variant)] opacity-35 mt-0.5">METRON internal calls</p>
              )}
            </div>
          ))}
        </div>
      </div>

      {/* Row 3 — Reliability */}
      <div>
        <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60 mb-3">Reliability</p>
        <div className="grid grid-cols-2 gap-4">
          <div className="p-4 rounded-xl bg-[var(--color-surface-container-low)] flex items-center gap-4">
            <span className="material-symbols-outlined text-3xl text-[var(--color-on-surface-variant)] opacity-40">content_cut</span>
            <div>
              <p className="font-headline text-xl font-black text-[var(--color-on-surface)]">
                {(truncation_rate * 100).toFixed(1)}%
                <span className="text-sm font-normal opacity-60 ml-1">({truncated_calls} calls)</span>
              </p>
              <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">Truncation Rate</p>
              <p className="text-[11px] text-[var(--color-on-surface-variant)] opacity-40 mt-0.5">Calls where max token limit was hit</p>
            </div>
          </div>
          <div className="p-4 rounded-xl bg-[var(--color-surface-container-low)] flex items-center gap-4">
            <span className="material-symbols-outlined text-3xl text-[var(--color-on-surface-variant)] opacity-40">replay</span>
            <div>
              <p className="font-headline text-xl font-black text-[var(--color-on-surface)]">{retry_count}</p>
              <p className="text-xs text-[var(--color-on-surface-variant)] opacity-60">Retry Count</p>
              <p className="text-[11px] text-[var(--color-on-surface-variant)] opacity-40 mt-0.5">Rate-limit backoffs + timeout retries</p>
            </div>
          </div>
        </div>
      </div>

      {/* Row 4 — Token breakdown by stage */}
      {stageEntries.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Token Breakdown by Stage</p>
          <div className="space-y-2">
            {stageEntries.map(([stage, s]) => {
              const barPct = Math.round((s.total_tokens / maxStageTokens) * 100);
              return (
                <div key={stage} className="flex items-center gap-3">
                  <span className="text-xs font-mono w-24 shrink-0 text-[var(--color-on-surface-variant)]">
                    {STAGE_LABELS[stage as keyof typeof STAGE_LABELS] ?? stage}
                  </span>
                  <div className="flex-1 h-5 rounded-full bg-[var(--color-surface-container-low)] overflow-hidden">
                    <div className="h-full rounded-full bg-primary opacity-70 transition-all" style={{ width: `${barPct}%` }} />
                  </div>
                  <span className="text-xs text-[var(--color-on-surface-variant)] opacity-70 w-40 shrink-0 text-right">
                    {s.calls} calls · {s.total_tokens.toLocaleString()} tokens
                  </span>
                </div>
              );
            })}
          </div>
        </div>
      )}

      {/* Row 5 — Input vs Output split */}
      {(total_prompt_tokens > 0 || total_completion_tokens > 0) && (
        <div className="space-y-2">
          <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Input vs Output Tokens</p>
          <div className="h-5 rounded-full overflow-hidden flex">
            <div className="h-full bg-primary opacity-70" style={{ width: `${promptPct}%` }} />
            <div className="h-full bg-secondary opacity-50 flex-1" />
          </div>
          <div className="flex justify-between text-xs text-[var(--color-on-surface-variant)] opacity-60">
            <span>Input: {total_prompt_tokens.toLocaleString()} ({promptPct}%)</span>
            <span>Output: {total_completion_tokens.toLocaleString()} ({100 - promptPct}%)</span>
          </div>
        </div>
      )}

      {/* Row 6 — Models used */}
      {modelEntries.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs font-bold uppercase tracking-widest text-[var(--color-on-surface-variant)] opacity-60">Models Used</p>
          <div className="flex flex-wrap gap-2">
            {modelEntries.map(([model, calls]) => (
              <span
                key={model}
                className="inline-flex items-center gap-1.5 px-3 py-1 rounded-full text-xs font-mono bg-[var(--color-surface-container-low)] text-[var(--color-on-surface-variant)]"
              >
                <span className="w-1.5 h-1.5 rounded-full bg-primary opacity-70 shrink-0" />
                {model}
                <span className="opacity-50 ml-0.5">· {calls} calls</span>
              </span>
            ))}
          </div>
        </div>
      )}

      {/* Footer */}
      {mlflowUrl && (
        <p className="text-xs text-[var(--color-on-surface-variant)] opacity-50 text-center">
          Full per-call traces available in{" "}
          <a href={mlflowUrl} target="_blank" rel="noopener noreferrer" className="underline hover:opacity-100">
            MLflow UI ↗
          </a>
        </p>
      )}
    </div>
  );
}
