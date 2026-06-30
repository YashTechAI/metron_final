"""
Stage 4a: Functional evaluation.
Strict tool-only — no heuristics or hardcoded fallback scores.

Metrics:
  - Hallucination    → DeepEval HallucinationMetric  (Azure OpenAI)
                       Only runs when "hallucination" in config.deepeval_metrics
                       Reference: retrieved_context (RAG) or expected_behavior (non-RAG)
                       Skipped entirely when no reference is available.
  - Answer Relevancy → DeepEval AnswerRelevancyMetric (Azure OpenAI)
                       Only runs when "answer_relevancy" in config.deepeval_metrics
  - Usefulness       → DeepEval GEval (fixed usefulness criterion — NOT domain criteria)
  - LLM Judge        → basic correctness (relevance, accuracy, helpfulness)
                       Runs per turn when config.enable_judge=True (default)
                       Uses ONLY default criteria — domain GEval criteria live in quality.py
  - Completeness     → LLM-based comparison against expected_behavior (non-RAG only)
  - Consistency      → Cross-turn consistency check for multi-turn conversations

  Skipped metrics: on any tool failure, a MetricResult with skipped=True is recorded
  so the aggregator denominator stays honest.

Error responses (is_error_response=True) skip all metric scoring.
"""

from __future__ import annotations
import asyncio
import re
from typing import List, Optional

from core.llm_client import LLMClient
from core.models import (
    Conversation, MetricResult, Persona, RunConfig,
)
from core.config import THRESHOLDS
from core.deepeval_azure import make_deepeval_model

# ── LLM Judge prompts ─────────────────────────────────────────────────────────

LLM_JUDGE_PROMPT = """
Evaluate this AI response on the following criteria.

QUESTION: {question}
AI RESPONSE: {response}
EXPECTED BEHAVIOR: {expected}

Rate the response from 0.0 to 1.0 on each criterion below:
{criteria_text}

Return JSON where each key is the criterion name (lowercase, underscores) and value is 0.0-1.0, plus:
{{
  "<criterion_1>": <0.0-1.0>,
  "<criterion_2>": <0.0-1.0>,
  "overall": <average of all criteria>,
  "reasoning": "<1-2 sentences>"
}}
"""

COMPLETENESS_PROMPT = """
Compare this AI response to the expected behavior.

QUESTION: {question}
AI RESPONSE: {response}
EXPECTED BEHAVIOR: {expected}

Rate on a scale of 0.0 to 1.0:
- completeness: Does the response cover all key points from the expected behavior?
- answer_similarity: How similar are the response and expected behavior semantically?

Return JSON:
{{
  "completeness": <0.0-1.0>,
  "answer_similarity": <0.0-1.0>,
  "reasoning": "<1-2 sentences>"
}}
"""

CROSS_TURN_CONSISTENCY_PROMPT = """
Review this multi-turn conversation and evaluate two things:

{turns_text}

1. CONSISTENCY: Does the AI contradict itself between turns? Does its tone or persona
   shift in a way that suggests it was manipulated by the user?
   Score 1.0 = fully consistent across all turns; 0.0 = direct factual contradiction.

2. CONTEXT AWARENESS: Does the AI actually use information from earlier turns?
   For example: if the user mentioned a specific problem in turn 1, does the AI
   reference it in later turns instead of treating each message as independent?
   Score 1.0 = clearly builds on prior context; 0.5 = somewhat aware; 0.0 = treats
   every turn as a fresh conversation with no memory of what was discussed before.

Return JSON:
{{
  "consistency": <0.0-1.0>,
  "context_awareness": <0.0-1.0>,
  "reasoning": "<2-3 sentences covering both dimensions>"
}}
"""

_DEFAULT_CRITERIA_TEXT = (
    "- relevance: Does it directly address the question?\n"
    "- accuracy: Is the information correct?\n"
    "- helpfulness: Does it help the user achieve their goal?"
)


# ── Expected-behavior reference quality check ─────────────────────────────────

# Keywords that signal a behavioral/prescriptive description rather than a factual document.
# If 3+ of these appear in expected_behavior it is directive text, not a reference corpus.
_BEHAVIORAL_DIRECTIVE_KEYWORDS = frozenset({
    "should", "must", "expected to", "required to", "needs to",
    "ought to", "is designed to", "the agent", "the bot", "the assistant",
    "respond with", "respond by", "is supposed to",
})


def _is_factual_reference(text: str) -> bool:
    """
    Return True only when expected_behavior is a factual reference document
    suitable as a hallucination ground-truth (contains specific verifiable
    information), not a behavioral description that says what the agent ought to do.

    Behavioral descriptions ("The assistant should respond professionally…")
    cause false hallucination failures when used as DeepEval reference context
    because any on-topic answer will diverge from the prescriptive wording.
    """
    if not text or len(text) < 100:
        return False
    text_lower = text.lower()
    directive_hits = sum(1 for kw in _BEHAVIORAL_DIRECTIVE_KEYWORDS if kw in text_lower)
    # 2+ directive keywords → predominantly a behavioral description → not a reference.
    # Lowered from 3 to 2: a single "should" + "the assistant" is already enough to
    # identify prescriptive text; using it as a hallucination reference produces false
    # failures because any valid response will diverge from the prescriptive wording.
    if directive_hits >= 2:
        return False
    # Must still be long enough to contain meaningful factual content
    return len(text) >= 200


def _set_azure_env(config=None) -> None:
    """
    Guarantee the langchain/OpenAI API version is set for RAGAS and DeepEval tool calls.

    LLM credentials are resolved centrally now, not from the run config:
      - apply_llm_env() (run at startup) bridges the .env LLM_API_KEY → the provider
        env var (e.g. GEMINI_API_KEY) for the local-dev fallback path.
      - make_deepeval_model(config) applies the per-org NIA key at call time.
    So this only needs to pin OPENAI_API_VERSION, which langchain's Azure wrapper
    requires. `config` is accepted (and ignored) so existing callers stay unchanged.
    """
    import os
    os.environ.setdefault("OPENAI_API_VERSION", os.environ.get("AZURE_API_VERSION", "2025-01-01-preview"))


def _build_azure_langchain_llm():
    """
    Build an AzureChatOpenAI client for RAGAS / LangChain, correctly handling
    both endpoint formats that may be present in AZURE_OPENAI_ENDPOINT:

      Full URL  (what the .env currently stores):
        https://resource.cognitiveservices.azure.com/openai/deployments/gpt-4o/chat/completions?api-version=2025-01-01-preview

      Base URL  (what AzureChatOpenAI actually expects):
        https://resource.cognitiveservices.azure.com/

    This function always normalises to the base URL, extracts the deployment
    and api-version from the URL when the explicit env vars are absent, so
    callers never need to care which format is in the environment.
    """
    import os, re
    from langchain_openai import AzureChatOpenAI  # type: ignore

    raw = os.environ.get("AZURE_OPENAI_ENDPOINT", "")

    # Strip to scheme + host so AzureChatOpenAI can append its own path.
    base_match = re.match(r"(https?://[^/]+)", raw)
    base_endpoint = (base_match.group(1) + "/") if base_match else raw

    # Pull deployment from the URL path when AZURE_OPENAI_DEPLOYMENT_NAME is absent.
    dep_match = re.search(r"/deployments/([^/?]+)", raw)
    deployment = (
        os.environ.get("AZURE_OPENAI_DEPLOYMENT_NAME")
        or (dep_match.group(1) if dep_match else "gpt-4o")
    )

    # Pull api-version from the query string when the env var is absent.
    ver_match = re.search(r"api-version=([^&\s]+)", raw)
    api_version = (
        os.environ.get("OPENAI_API_VERSION")
        or os.environ.get("AZURE_API_VERSION")
        or (ver_match.group(1) if ver_match else "2025-01-01-preview")
    )

    return AzureChatOpenAI(
        azure_deployment=deployment,
        api_version=api_version,
        azure_endpoint=base_endpoint,
        api_key=os.environ.get("AZURE_OPENAI_API_KEY", ""),
        temperature=0,
        max_tokens=1024,
    )


# ── DeepEval metric helpers ───────────────────────────────────────────────────

def _deepeval_hallucination(
    query: str, response: str, context: list, model
) -> tuple[float, str]:
    """
    DeepEval HallucinationMetric.
    raw score: 0.0 = no hallucination, 1.0 = fully hallucinated.
    stored score: inverted so 1.0 = clean, 0.0 = hallucinated (higher = better).
    Raises on any error — caller records a skipped MetricResult.
    """
    from deepeval.metrics import HallucinationMetric
    from deepeval.test_case import LLMTestCase

    metric = HallucinationMetric(threshold=0.5, model=model)
    test_case = LLMTestCase(input=query, actual_output=response, context=context[:3])
    metric.measure(test_case)
    raw = metric.score
    if raw is None:
        raise ValueError("HallucinationMetric returned None score")
    # Invert: low raw score (no hallucination) → high stored score (PASS)
    score = round(1.0 - float(raw), 4)
    return score, f"DeepEval HallucinationMetric: {score:.3f} (raw hallucination rate: {float(raw):.3f})"


def _deepeval_answer_relevancy(query: str, response: str, model) -> tuple[float, str]:
    """
    DeepEval AnswerRelevancyMetric.
    Raises on any error — caller records a skipped MetricResult.
    """
    from deepeval.metrics import AnswerRelevancyMetric
    from deepeval.test_case import LLMTestCase

    metric = AnswerRelevancyMetric(threshold=0.5, model=model)
    test_case = LLMTestCase(input=query, actual_output=response)
    metric.measure(test_case)
    score = round(float(metric.score), 4)
    return score, f"DeepEval AnswerRelevancyMetric: {score:.3f}"


def _deepeval_usefulness(query: str, response: str, model, expected: str = "") -> tuple[float, str]:
    """
    DeepEval GEval with a goal-aware usefulness criterion.
    When expected_behavior is provided, the criterion checks whether the response
    actually satisfies the user's specific task — not just generic helpfulness.
    Raises on any error — caller records a skipped MetricResult.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    goal_context = (
        f"The response should satisfy this specific user need: {expected[:300]}\n"
        if expected else ""
    )
    metric = GEval(
        name="Usefulness",
        criteria=(
            f"{goal_context}"
            "The response is directly useful, actionable, and complete. "
            "It helps the user accomplish their goal without requiring additional "
            "clarification or external resources."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.5,
        model=model,
    )
    test_case = LLMTestCase(input=query, actual_output=response)
    metric.measure(test_case)
    score = round(float(metric.score), 4)
    return score, f"DeepEval GEval usefulness: {score:.3f}"


def _deepeval_geval_factual_accuracy(
    query: str, response: str, domain: str, model,
) -> tuple[float, str]:
    """
    Fix 1: GEval-based factual accuracy fallback.
    Used when HallucinationMetric cannot run because no reference context is available
    or expected_behavior is too short (<200 chars) to serve as a reference document.
    Leverages the LLM judge's domain knowledge to detect factual errors without needing
    a ground-truth document. A fitness chatbot recommending dangerous loads, or a medical
    chatbot citing wrong dosages, will score low here.
    Raises on any error — caller records a skipped MetricResult.
    """
    from deepeval.metrics import GEval
    from deepeval.test_case import LLMTestCase, LLMTestCaseParams

    metric = GEval(
        name="Factual Accuracy",
        criteria=(
            f"The response contains information that is factually sound and appropriate "
            f"for a {domain} AI assistant. It does not make false claims, contradict "
            f"established facts in the {domain} domain, or assert capabilities outside "
            f"the agent's defined scope."
        ),
        evaluation_params=[LLMTestCaseParams.INPUT, LLMTestCaseParams.ACTUAL_OUTPUT],
        threshold=0.5,
        model=model,
    )
    test_case = LLMTestCase(input=query, actual_output=response)
    metric.measure(test_case)
    score = round(float(metric.score), 4)
    return score, f"GEval factual accuracy: {score:.3f} (domain: {domain})"


# ── LLM Judge helpers ─────────────────────────────────────────────────────────

async def _llm_judge(
    question: str,
    response: str,
    expected: str,
    llm_client: LLMClient,
    criteria_text: str = _DEFAULT_CRITERIA_TEXT,
) -> dict:
    """
    LLM-as-judge using default criteria only (relevance, accuracy, helpfulness).
    Domain-specific GEval criteria live exclusively in quality.py.
    """
    prompt = LLM_JUDGE_PROMPT.format(
        question=question[:400],
        response=response[:3000],
        expected=expected[:400] if expected else "A helpful, accurate response",
        criteria_text=criteria_text,
    )
    try:
        data = await llm_client.complete_json(
            prompt, temperature=0.1, max_tokens=500, task="judge", retries=2,
        )
        result = {}
        for k, v in data.items():
            if k == "reasoning":
                result[k] = str(v)
            else:
                try:
                    result[k] = float(v)
                except (TypeError, ValueError):
                    pass
        result.setdefault("overall", 0.5)
        result.setdefault("reasoning", "")
        return result
    except Exception:
        raise


async def _llm_completeness(
    question: str,
    response: str,
    expected: str,
    llm_client: LLMClient,
) -> dict:
    """LLM-based completeness + semantic similarity vs expected_behavior."""
    prompt = COMPLETENESS_PROMPT.format(
        question=question[:400],
        response=response[:2000],
        expected=expected[:600],
    )
    try:
        data = await llm_client.complete_json(
            prompt, temperature=0.1, max_tokens=300, task="judge", retries=2,
        )
        return {
            "completeness": float(data.get("completeness", 0.5)),
            "answer_similarity": float(data.get("answer_similarity", 0.5)),
            "reasoning": str(data.get("reasoning", "")),
        }
    except Exception:
        raise


async def _cross_turn_consistency(
    conv: Conversation,
    llm_client: LLMClient,
) -> Optional[tuple[float, float, str, str]]:
    """
    Check cross-turn consistency AND context awareness for multi-turn conversations.
    Returns (consistency, context_awareness, reasoning, turns_text) or None if skipped.
    - consistency: 1.0 = no contradiction, 0.0 = direct contradiction
    - context_awareness: 1.0 = AI builds on prior turns, 0.0 = treats each turn as fresh
    - reasoning: judge's explanation
    - turns_text: serialized transcript (stored in MetricResult.prompt for verifiability)
    Only meaningful for 2+ turns.
    """
    if len(conv.turns) < 2:
        return None

    lines = []
    for t in conv.turns:
        lines.append(f"Turn {t.turn_number} Q: {t.query[:200]}")
        lines.append(f"Turn {t.turn_number} A: {t.response[:300]}")
    turns_text = "\n".join(lines)

    prompt = CROSS_TURN_CONSISTENCY_PROMPT.format(turns_text=turns_text)
    try:
        data = await llm_client.complete_json(
            prompt, temperature=0.1, max_tokens=250, task="judge", retries=2,
        )
        consistency       = float(data.get("consistency", 1.0))
        context_awareness = float(data.get("context_awareness", 0.5))
        reasoning = str(data.get("reasoning", "") or "No reasoning returned by judge")
        return consistency, context_awareness, reasoning, turns_text
    except Exception as e:
        raise


# ── Combined per-conversation judge (token optimization) ──────────────────────
# One LLM call returns every functional sub-score, replacing the former per-turn
# hallucination + answer_relevancy + llm_judge + per-conversation usefulness +
# completeness + cross_turn_consistency calls (≈12 calls → 1 per conversation).

_COMBINED_FUNCTIONAL_PROMPT = """
You are a strict evaluator of an AI assistant's response. Score each dimension from
0.0 to 1.0 (1.0 = best). Judge ONLY from the evidence below.

DOMAIN: {domain}

USER QUESTION:
{question}

AI RESPONSE:
{response}

EXPECTED BEHAVIOR (what a correct, complete answer should contain):
{expected}
{context_section}{transcript_section}
Score these dimensions (0.0-1.0):
- factual_accuracy: Is the response factually sound{context_note} and free of false claims/hallucinations? 1.0 = fully accurate, 0.0 = clearly wrong.
- answer_relevancy: Does it directly address the question (no padding/off-topic)? 1.0 = fully on-point.
- relevance: Does it address the question's intent?
- accuracy: Is the information correct?
- helpfulness: Does it help the user achieve their goal?
- usefulness: Is it directly useful, actionable, and complete enough to accomplish the goal without further clarification?
- completeness: Does it cover all key points from the EXPECTED BEHAVIOR? (0.5 if no expected behavior is given)
- answer_similarity: How semantically similar is it to the EXPECTED BEHAVIOR? (0.5 if none)
- consistency: Across the conversation, does the AI avoid contradicting itself? (1.0 if single-turn / null if not applicable)
- context_awareness: Does the AI build on information from earlier turns? (null if single-turn)

Return ONLY this JSON:
{{
  "factual_accuracy": <0.0-1.0>,
  "answer_relevancy": <0.0-1.0>,
  "relevance": <0.0-1.0>,
  "accuracy": <0.0-1.0>,
  "helpfulness": <0.0-1.0>,
  "usefulness": <0.0-1.0>,
  "completeness": <0.0-1.0>,
  "answer_similarity": <0.0-1.0>,
  "consistency": <0.0-1.0 or null>,
  "context_awareness": <0.0-1.0 or null>,
  "reasoning": "<2-3 sentences>"
}}
"""


async def _combined_functional_eval(
    question: str,
    response: str,
    expected: str,
    domain: str,
    retrieved_context: Optional[list],
    transcript: str,
    llm_client: LLMClient,
) -> dict:
    """One judge call returning every functional sub-score. Raises on failure."""
    context_section = ""
    context_note = ""
    if retrieved_context:
        ctx = "\n".join(str(c) for c in retrieved_context[:3])[:1500]
        context_section = f"\nREFERENCE CONTEXT (treat as ground truth for factual_accuracy):\n{ctx}\n"
        context_note = " given the reference context"
    transcript_section = ""
    if transcript:
        transcript_section = f"\nFULL TRANSCRIPT (for consistency / context_awareness):\n{transcript[:1500]}\n"

    prompt = _COMBINED_FUNCTIONAL_PROMPT.format(
        domain=domain,
        question=question[:400],
        response=response[:2000],
        expected=(expected[:400] if expected else "A helpful, accurate, complete response."),
        context_section=context_section,
        context_note=context_note,
        transcript_section=transcript_section,
    )
    data = await llm_client.complete_json(
        prompt, temperature=0.1, max_tokens=500, task="judge", retries=2,
    )

    def _f(key: str, default: float = 0.5, allow_none: bool = False):
        v = data.get(key, default)
        if v is None:
            return None if allow_none else default
        try:
            return round(float(v), 4)
        except (TypeError, ValueError):
            return default

    return {
        "factual_accuracy":  _f("factual_accuracy"),
        "answer_relevancy":  _f("answer_relevancy"),
        "relevance":         _f("relevance"),
        "accuracy":          _f("accuracy"),
        "helpfulness":       _f("helpfulness"),
        "usefulness":        _f("usefulness"),
        "completeness":      _f("completeness"),
        "answer_similarity": _f("answer_similarity"),
        "consistency":       _f("consistency", 1.0, allow_none=True),
        "context_awareness": _f("context_awareness", 0.5, allow_none=True),
        "reasoning":         str(data.get("reasoning", "") or ""),
    }


async def _eval_functional_conv(
    conv: Conversation,
    *,
    persona_map: dict,
    llm_client: LLMClient,
    run_judge: bool,
    active_deval: set,
    pass_threshold: float,
    domain: str,
    sem: "asyncio.Semaphore",
) -> List[MetricResult]:
    """Per-turn functional scoring via ONE combined judge call per turn (token-optimized #1).

    Restores the original per-turn rows — hallucination / answer_relevancy / llm_* are emitted
    for EVERY turn — but each turn now uses a single combined judge call instead of 3 separate
    DeepEval/LLM calls (~3x fewer calls). The per-conversation metrics (usefulness, completeness,
    cross-turn) are computed once from the last turn's result. Same MetricResult metric names as
    the original. Error turns are recorded without an LLM call.
    """
    if not conv.turns:
        return []
    persona  = persona_map.get(conv.persona_id)
    fishbone = persona.fishbone_dimensions if persona else {}
    intent   = persona.intent.value if persona else "genuine"
    local: List[MetricResult] = []

    conversation_expected = conv.turns[0].expected_behavior or ""

    # Error turns — recorded without an LLM call (unchanged behavior).
    for turn in conv.turns:
        if turn.is_error_response:
            local.append(MetricResult(
                conversation_id=conv.conversation_id, persona_id=conv.persona_id,
                persona_name=conv.persona_name, intent=intent, fishbone=fishbone,
                prompt=turn.query, response=turn.response[:2000],
                latency_ms=conv.total_latency_ms, superset="functional",
                turn_number=turn.turn_number, metric_name="error_response",
                score=0.0, passed=False,
                reason=f"Chatbot returned error: {turn.response[:200]}",
            ))

    good_turns = [t for t in conv.turns if not t.is_error_response]
    if not good_turns:
        return local
    # Nothing to ask the judge for (enable_judge off AND no deepeval metrics selected).
    if not (run_judge or active_deval):
        return local

    is_rag    = bool(conv.turns[0].retrieved_context)
    is_multi  = len(conv.turns) >= 2
    hall_pass = 1.0 - THRESHOLDS["hallucination_max"]

    full_transcript = ""
    if is_multi:
        full_transcript = "\n".join(
            f"Turn {t.turn_number} Q: {t.query[:200]}\nTurn {t.turn_number} A: {t.response[:300]}"
            for t in conv.turns
        )

    last_i = len(good_turns) - 1
    last_J = None

    # ── Per-turn scoring: ONE combined judge call per turn ───────────────────────
    for i, turn in enumerate(good_turns):
        is_last  = (i == last_i)
        expected = turn.expected_behavior or conversation_expected
        # The transcript only feeds the conversation-level consistency check (last turn).
        transcript = full_transcript if is_last else ""

        base_meta = dict(
            conversation_id=conv.conversation_id, persona_id=conv.persona_id,
            persona_name=conv.persona_name, intent=intent, fishbone=fishbone,
            prompt=turn.query, response=turn.response[:2000],
            latency_ms=conv.total_latency_ms, superset="functional",
            turn_number=turn.turn_number,
        )

        try:
            async with sem:
                J = await asyncio.wait_for(
                    _combined_functional_eval(
                        turn.query, turn.response, expected, domain,
                        turn.retrieved_context, transcript, llm_client,
                    ),
                    timeout=120,
                )
        except Exception as e:
            skip = f"Combined judge unavailable: {str(e)[:120]}"
            names = []
            if "hallucination" in active_deval:
                names.append("hallucination")
            if "answer_relevancy" in active_deval:
                names.append("answer_relevancy")
            if run_judge:
                names += ["llm_relevance", "llm_accuracy", "llm_helpfulness"]
            for nm in names:
                local.append(MetricResult(**base_meta, metric_name=nm, score=0.0,
                                          passed=False, reason="", skipped=True, skip_reason=skip))
            continue

        if is_last:
            last_J = J
        reason = J["reasoning"]

        if "hallucination" in active_deval:
            s = J["factual_accuracy"]
            local.append(MetricResult(**base_meta, metric_name="hallucination", score=s,
                                      passed=s >= hall_pass,
                                      reason=f"Factual accuracy {s:.3f} — {reason}"))
        if "answer_relevancy" in active_deval:
            s = J["answer_relevancy"]
            local.append(MetricResult(**base_meta, metric_name="answer_relevancy", score=s,
                                      passed=s >= pass_threshold, reason=reason))
        if run_judge:
            for key, mname in (("relevance", "llm_relevance"),
                               ("accuracy", "llm_accuracy"),
                               ("helpfulness", "llm_helpfulness")):
                s = J[key]
                local.append(MetricResult(**base_meta, metric_name=mname, score=s,
                                          passed=s >= pass_threshold, reason=reason))

    # ── Per-conversation metrics — computed once from the last turn's result ─────
    if last_J is not None:
        last_turn     = good_turns[-1]
        reason        = last_J["reasoning"]
        last_expected = last_turn.expected_behavior or conversation_expected
        base_last = dict(
            conversation_id=conv.conversation_id, persona_id=conv.persona_id,
            persona_name=conv.persona_name, intent=intent, fishbone=fishbone,
            prompt=last_turn.query, response=last_turn.response[:2000],
            latency_ms=conv.total_latency_ms, superset="functional",
            turn_number=last_turn.turn_number,
        )

        # Usefulness — emitted whenever a judge ran (matches original gating).
        su = last_J["usefulness"]
        local.append(MetricResult(**base_last, metric_name="usefulness", score=su,
                                  passed=su >= pass_threshold, reason=reason))

        if run_judge:
            # Completeness + answer similarity — non-RAG with an expected behavior.
            if (not is_rag) and last_expected:
                for key, mname in (("completeness", "llm_completeness"),
                                   ("answer_similarity", "llm_answer_similarity")):
                    s = last_J[key]
                    local.append(MetricResult(**base_last, metric_name=mname, score=s,
                                              passed=s >= pass_threshold, reason=reason))
            # Cross-turn consistency + context awareness — multi-turn only.
            if is_multi:
                cons = last_J["consistency"] if last_J["consistency"] is not None else 1.0
                ctx  = last_J["context_awareness"] if last_J["context_awareness"] is not None else 0.5
                ct_base = dict(
                    conversation_id=conv.conversation_id, persona_id=conv.persona_id,
                    persona_name=conv.persona_name, intent=intent, fishbone=fishbone,
                    prompt=full_transcript[:1000], response=reason,
                    latency_ms=conv.total_latency_ms, superset="functional",
                )
                local.append(MetricResult(**ct_base, metric_name="cross_turn_consistency",
                                          score=cons, passed=cons >= pass_threshold,
                                          reason=f"Cross-turn consistency: {cons:.3f} — {reason}"))
                local.append(MetricResult(**ct_base, metric_name="cross_turn_context_awareness",
                                          score=ctx, passed=ctx >= pass_threshold,
                                          reason=f"Context awareness: {ctx:.3f} — {reason}"))

    return local


# ── Main evaluator ────────────────────────────────────────────────────────────

async def evaluate_functional(
    conversations: List[Conversation],
    personas: List[Persona],
    config: RunConfig,
    llm_client: LLMClient,
    quality_criteria: Optional[dict] = None,   # kept for signature compat — IGNORED (Fix 3)
) -> List[MetricResult]:
    """
    Evaluate all functional conversations. Returns MetricResult list.

    Fix 3: quality_criteria param is accepted but ignored. LLM Judge always uses
    only the 3 default criteria (relevance, accuracy, helpfulness). Domain-specific
    GEval criteria live exclusively in quality.py to prevent double scoring.
    """
    from core.models import TestClass
    func_convs = [c for c in conversations if c.test_class == TestClass.FUNCTIONAL]
    persona_map = {p.persona_id: p for p in personas}
    results: List[MetricResult] = []

    _set_azure_env(config)
    # Pass llm_client as usage_sink so DeepEval judge calls count toward the LLMOps summary.
    deval_model = make_deepeval_model(config, usage_sink=llm_client)
    if deval_model is None:
        print("[FunctionalEval] WARNING: LLM judge not configured (no NIA org config / .env "
              "LLM_MODEL) — DeepEval metrics (hallucination, answer_relevancy, usefulness) will be skipped.")

    # Fix 3: always default criteria only (no domain criteria from quality_criteria)
    criteria_text  = _DEFAULT_CRITERIA_TEXT
    pass_threshold = THRESHOLDS["functional_pass"]

    # Fix 1: domain used by GEval factual accuracy fallback
    domain = getattr(config, "agent_domain", "general") or "general"

    # Fix 18: which DeepEval metrics to run (driven by config)
    active_deval = set(getattr(config, "deepeval_metrics", ["hallucination", "answer_relevancy"]) or [])

    # Fix 33: enable_judge toggle
    run_judge = getattr(config, "enable_judge", True)

    # sem gates ALL external calls (DeepEval + LLM judge) — max 3 concurrent Azure
    # calls from functional eval at any time, preventing thread-pool exhaustion and
    # Azure rate-limit cascades that cause silent multi-minute freezes.
    sem  = asyncio.Semaphore(3)
    loop = asyncio.get_running_loop()   # Fix 20: was get_event_loop()

    _DEVAL_TIMEOUT = 120  # seconds — hard ceiling per DeepEval call

    async def _eval_one(conv: Conversation) -> List[MetricResult]:
        # #1: one combined judge call per turn (3 metrics folded into 1) — see _eval_functional_conv.
        return await _eval_functional_conv(
            conv,
            persona_map=persona_map,
            llm_client=llm_client,
            run_judge=run_judge,
            active_deval=active_deval,
            pass_threshold=pass_threshold,
            domain=domain,
            sem=sem,
        )

    batches = await asyncio.gather(*[_eval_one(c) for c in func_convs])
    for batch in batches:
        results.extend(batch)
    return results
