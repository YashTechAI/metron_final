"""Contract tests for the token-optimized combined evaluators (#1+#2+#3).

Verify that the new single-call-per-conversation judges:
  - make exactly ONE llm_client.complete_json call per conversation,
  - emit the SAME MetricResult metric names the per-turn/per-criterion code did,
  - map sub-scores to the right metrics with correct pass/fail.
"""
import asyncio
import os
import sys
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import Conversation, ConversationTurn, TestClass
from stages.s4_evaluation.functional import _eval_functional_conv
from stages.s4_evaluation.quality import _combined_quality_geval


def _run(coro):
    return asyncio.get_event_loop().run_until_complete(coro)


def _turn(n, q, a, expected=None, error=False, context=None):
    return ConversationTurn(
        turn_number=n, query=q, response=a, latency_ms=10.0,
        expected_behavior=expected, is_error_response=error,
        retrieved_context=context,
    )


def _conv(turns):
    c = Conversation(persona_id="p1", persona_name="Tester", test_class=TestClass.FUNCTIONAL)
    c.turns = turns
    c.total_latency_ms = sum(t.latency_ms for t in turns)
    return c


_FULL_J = {
    "factual_accuracy": 0.90, "answer_relevancy": 0.80, "relevance": 0.70,
    "accuracy": 0.60, "helpfulness": 0.50, "usefulness": 0.40,
    "completeness": 0.30, "answer_similarity": 0.20,
    "consistency": 0.95, "context_awareness": 0.85, "reasoning": "because",
}


class TestFunctionalCombined:
    def test_one_call_and_all_metric_names(self):
        llm = MagicMock()
        llm.complete_json = AsyncMock(return_value=dict(_FULL_J))
        conv = _conv([_turn(1, "Q1", "A1", expected="exp1"), _turn(2, "Q2", "A2")])

        async def go():
            sem = asyncio.Semaphore(3)
            return await _eval_functional_conv(
                conv, persona_map={}, llm_client=llm, run_judge=True,
                active_deval={"hallucination", "answer_relevancy"},
                pass_threshold=0.5, domain="general", sem=sem,
            )

        results = _run(go())

        # The whole point of #1+#2: exactly ONE judge call for the conversation.
        assert llm.complete_json.call_count == 1

        names = {r.metric_name for r in results}
        assert names == {
            "hallucination", "answer_relevancy",
            "llm_relevance", "llm_accuracy", "llm_helpfulness", "usefulness",
            "llm_completeness", "llm_answer_similarity",
            "cross_turn_consistency", "cross_turn_context_awareness",
        }

        by = {r.metric_name: r for r in results}
        assert by["hallucination"].score == 0.90 and by["hallucination"].passed is True
        assert by["usefulness"].score == 0.40 and by["usefulness"].passed is False
        assert by["llm_answer_similarity"].score == 0.20 and by["llm_answer_similarity"].passed is False
        assert by["cross_turn_consistency"].score == 0.95 and by["cross_turn_consistency"].passed is True

    def test_error_turn_no_judge_call(self):
        llm = MagicMock()
        llm.complete_json = AsyncMock(return_value=dict(_FULL_J))
        conv = _conv([_turn(1, "Q1", "[Error: boom]", error=True)])

        async def go():
            sem = asyncio.Semaphore(3)
            return await _eval_functional_conv(
                conv, persona_map={}, llm_client=llm, run_judge=True,
                active_deval={"hallucination", "answer_relevancy"},
                pass_threshold=0.5, domain="general", sem=sem,
            )

        results = _run(go())
        assert llm.complete_json.call_count == 0          # all turns errored → no judge call
        assert [r.metric_name for r in results] == ["error_response"]

    def test_rag_skips_completeness_similarity(self):
        llm = MagicMock()
        llm.complete_json = AsyncMock(return_value=dict(_FULL_J))
        conv = _conv([_turn(1, "Q1", "A1", expected="exp1", context=["doc chunk"])])

        async def go():
            sem = asyncio.Semaphore(3)
            return await _eval_functional_conv(
                conv, persona_map={}, llm_client=llm, run_judge=True,
                active_deval={"hallucination", "answer_relevancy"},
                pass_threshold=0.5, domain="general", sem=sem,
            )

        results = _run(go())
        names = {r.metric_name for r in results}
        # RAG conversation → completeness/answer_similarity are not emitted; single-turn → no cross_turn.
        assert "llm_completeness" not in names and "llm_answer_similarity" not in names
        assert "cross_turn_consistency" not in names
        assert "hallucination" in names and "usefulness" in names


class TestQualityCombined:
    def test_one_call_scores_and_weighted_overall(self):
        llm = MagicMock()
        llm.complete_json = AsyncMock(return_value={
            "scores": {"Clarity": 0.8, "Tone": 0.4}, "reasoning": "x",
        })
        criteria = [
            {"name": "Clarity", "description": "Is the answer clearly written?", "weight": 1.0},
            {"name": "Tone",    "description": "Is the tone appropriate?",       "weight": 2.0},
        ]
        res = _run(_combined_quality_geval(
            "How do I reset my password?", "Click reset.", criteria,
            {"Clarity": 1.0, "Tone": 2.0}, llm,
        ))
        assert llm.complete_json.call_count == 1          # one call for ALL criteria (#3)
        assert res["scores"] == {"Clarity": 0.8, "Tone": 0.4}
        assert res["method"] == "combined_rubric"
        # weighted overall = (0.8*1 + 0.4*2) / 3 = 0.5333
        assert abs(res["overall"] - 0.5333) < 0.001
