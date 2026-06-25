"""
Tests for multi-turn scenario chaining.

Covers:
  - functional_gen: multi_turn_scenario creates 1 starter + scenario_turns, not 3 separate prompts
  - conversation_runner: scenario_turns replayed in-sequence before dynamic generation
  - models: GeneratedPrompt.scenario_turns field exists with correct default
"""

import asyncio
import sys
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from typing import List

# Make sure the project root is on the path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.models import (
    ApplicationType, Conversation, ConversationTurn, ConversationState,
    ExpertiseLevel, EmotionalState, GeneratedPrompt, Persona, PersonaIntent,
    RunConfig, TestClass, BehavioralParameters, LanguageModel,
)


# ── Helpers ────────────────────────────────────────────────────────────────────

def make_persona(multi_turn_scenario=None, entry_points=None) -> Persona:
    return Persona(
        name="Test User",
        user_type="end_user",
        expertise=ExpertiseLevel.INTERMEDIATE,
        emotional_state=EmotionalState.CALM,
        intent=PersonaIntent.GENUINE,
        background="A regular user",
        goal="Get help with the product",
        language_model=LanguageModel(base_style="conversational", frustrated_style="more direct"),
        behavioral_params=BehavioralParameters(),
        multi_turn_scenario=multi_turn_scenario or [],
        entry_points=entry_points or [],
    )


def make_run_config(conversation_turns: int = 5) -> RunConfig:
    return RunConfig(
        endpoint_url="http://test.local/chat",
        agent_domain="general",
        conversation_turns=conversation_turns,
        enable_judge=False,
        llm_provider="Groq",
        llm_api_key="test-key",
    )


# ── Model field tests ──────────────────────────────────────────────────────────

class TestGeneratedPromptModel:
    def test_scenario_turns_defaults_to_empty_list(self):
        prompt = GeneratedPrompt(
            persona_id="abc",
            test_class=TestClass.FUNCTIONAL,
            text="hello",
        )
        assert prompt.scenario_turns == []

    def test_scenario_turns_stores_dicts(self):
        turns = [
            {"turn": 2, "prompt": "follow up", "expected_behavior": "should answer"},
            {"turn": 3, "prompt": "another follow up", "expected_behavior": "should clarify"},
        ]
        prompt = GeneratedPrompt(
            persona_id="abc",
            test_class=TestClass.FUNCTIONAL,
            text="first message",
            scenario_turns=turns,
        )
        assert len(prompt.scenario_turns) == 2
        assert prompt.scenario_turns[0]["prompt"] == "follow up"
        assert prompt.scenario_turns[1]["turn"] == 3

    def test_security_prompt_has_no_scenario_turns(self):
        prompt = GeneratedPrompt(
            persona_id="abc",
            test_class=TestClass.SECURITY,
            text="attack prompt",
            attack_category="jailbreak",
        )
        assert prompt.scenario_turns == []


# ── functional_gen tests ───────────────────────────────────────────────────────

class TestFunctionalGenScenarioChaining:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_multi_turn_scenario_creates_one_starter_prompt(self):
        from stages.s2_tests.functional_gen import generate_functional_prompts
        from core.models import AppProfile

        scenario = [
            {"turn": 1, "prompt": "Hello I need help", "expected_behavior": "Greet and assist"},
            {"turn": 2, "prompt": "Can you be more specific", "expected_behavior": "Provide details"},
            {"turn": 3, "prompt": "What about X", "expected_behavior": "Explain X"},
        ]
        persona = make_persona(multi_turn_scenario=scenario, entry_points=["entry point 1"])
        profile = AppProfile(domain="general", use_cases=["help users"], boundaries=[])
        llm_client = MagicMock()

        prompts = self._run(generate_functional_prompts(persona, profile, llm_client))

        # Should produce 2 prompts: 1 chained starter + 1 entry_point (not 3 separate starts)
        assert len(prompts) == 2

    def test_multi_turn_scenario_first_prompt_is_turn1(self):
        from stages.s2_tests.functional_gen import generate_functional_prompts
        from core.models import AppProfile

        scenario = [
            {"turn": 1, "prompt": "First message", "expected_behavior": "First expected"},
            {"turn": 2, "prompt": "Second message", "expected_behavior": "Second expected"},
            {"turn": 3, "prompt": "Third message", "expected_behavior": "Third expected"},
        ]
        persona = make_persona(multi_turn_scenario=scenario)
        profile = AppProfile(domain="general", use_cases=["help"], boundaries=[])
        llm_client = MagicMock()

        prompts = self._run(generate_functional_prompts(persona, profile, llm_client))

        # First (and only scenario) prompt should be turn 1
        starter = prompts[0]
        assert starter.text == "First message"
        assert starter.expected_behavior == "First expected"
        assert starter.turn_number == 1

    def test_multi_turn_scenario_scenario_turns_attached(self):
        from stages.s2_tests.functional_gen import generate_functional_prompts
        from core.models import AppProfile

        scenario = [
            {"turn": 1, "prompt": "First", "expected_behavior": "Exp1"},
            {"turn": 2, "prompt": "Second", "expected_behavior": "Exp2"},
            {"turn": 3, "prompt": "Third", "expected_behavior": "Exp3"},
        ]
        persona = make_persona(multi_turn_scenario=scenario)
        profile = AppProfile(domain="general", use_cases=["help"], boundaries=[])
        llm_client = MagicMock()

        prompts = self._run(generate_functional_prompts(persona, profile, llm_client))

        starter = prompts[0]
        assert len(starter.scenario_turns) == 2
        assert starter.scenario_turns[0]["prompt"] == "Second"
        assert starter.scenario_turns[1]["prompt"] == "Third"
        assert starter.scenario_turns[0]["expected_behavior"] == "Exp2"

    def test_entry_points_added_as_standalone_prompts(self):
        from stages.s2_tests.functional_gen import generate_functional_prompts
        from core.models import AppProfile

        scenario = [
            {"turn": 1, "prompt": "Start", "expected_behavior": "Exp"},
            {"turn": 2, "prompt": "Follow up", "expected_behavior": "Exp2"},
        ]
        persona = make_persona(
            multi_turn_scenario=scenario,
            entry_points=["Entry point 1", "Entry point 2"],
        )
        profile = AppProfile(domain="general", use_cases=["help"], boundaries=[])
        llm_client = MagicMock()

        prompts = self._run(generate_functional_prompts(persona, profile, llm_client))

        texts = [p.text for p in prompts]
        assert "Entry point 1" in texts
        assert "Entry point 2" in texts
        # All entry_points should have empty scenario_turns
        for p in prompts[1:]:
            assert p.scenario_turns == []

    def test_empty_scenario_falls_through_to_llm(self):
        from stages.s2_tests.functional_gen import generate_functional_prompts
        from core.models import AppProfile

        persona = make_persona(multi_turn_scenario=[])
        profile = AppProfile(domain="general", use_cases=["help"], boundaries=[])

        llm_client = MagicMock()
        llm_client.complete_json = AsyncMock(return_value={
            "prompts": [
                {"text": "LLM generated", "expected_behavior": "exp", "turn": 1},
            ]
        })

        prompts = self._run(generate_functional_prompts(persona, profile, llm_client))

        assert any(p.text == "LLM generated" for p in prompts)


# ── conversation_runner tests ──────────────────────────────────────────────────

class TestConversationRunnerScenarioChaining:
    def _run(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def _make_adapter_response(self, text="OK response"):
        resp = MagicMock()
        resp.ok = True
        resp.text = text
        resp.error = None
        resp.latency_ms = 50.0
        resp.retrieved_context = None
        resp.agent_trace = None
        return resp

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_scenario_turns_replayed_before_dynamic(self, mock_eval_gen, mock_get_adapter):
        from stages.s3_execution.conversation_runner import run_conversation

        # Adapter returns OK for every call
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=self._make_adapter_response())
        mock_get_adapter.return_value = adapter

        # State machine should NOT be called for scenario turns
        mock_eval_gen.return_value = (None, ConversationState.SATISFIED, None, True, None)

        scenario_turns = [
            {"turn": 2, "prompt": "Follow up question", "expected_behavior": "Exp2"},
            {"turn": 3, "prompt": "Third message", "expected_behavior": "Exp3"},
        ]
        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First message",
            expected_behavior="Exp1",
            scenario_turns=scenario_turns,
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config(conversation_turns=5)

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        # Should have sent 3 messages (turn 1 + 2 scenario turns)
        assert adapter.send.call_count >= 3

        # Check the queries sent match the scenario prompts
        calls = adapter.send.call_args_list
        queries = [c[0][0] for c in calls]
        assert "First message" in queries
        assert "Follow up question" in queries
        assert "Third message" in queries

        # Dynamic state machine called only AFTER scenario turns exhausted (or not at all if satisfied)
        # mock_eval_gen call count should be 0 or 1 (only for turn 4+ if not already done)
        assert mock_eval_gen.call_count <= 1

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_scenario_turns_carry_expected_behavior_to_turns(self, mock_eval_gen, mock_get_adapter):
        from stages.s3_execution.conversation_runner import run_conversation

        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=self._make_adapter_response())
        mock_get_adapter.return_value = adapter
        mock_eval_gen.return_value = (None, ConversationState.SATISFIED, None, True, None)

        scenario_turns = [
            {"turn": 2, "prompt": "Second", "expected_behavior": "Exp for turn 2"},
        ]
        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First",
            expected_behavior="Exp for turn 1",
            scenario_turns=scenario_turns,
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config(conversation_turns=3)

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        assert len(conv.turns) >= 2
        assert conv.turns[0].expected_behavior == "Exp for turn 1"
        assert conv.turns[1].expected_behavior == "Exp for turn 2"

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_error_on_turn1_stops_before_scenario_turns(self, mock_eval_gen, mock_get_adapter):
        from stages.s3_execution.conversation_runner import run_conversation

        error_resp = MagicMock()
        error_resp.ok = False
        error_resp.text = "[Error: connection refused]"
        error_resp.error = "connection refused"
        error_resp.latency_ms = 10.0
        error_resp.retrieved_context = None
        error_resp.agent_trace = None

        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=error_resp)
        mock_get_adapter.return_value = adapter

        scenario_turns = [
            {"turn": 2, "prompt": "Follow up", "expected_behavior": "exp"},
        ]
        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First",
            scenario_turns=scenario_turns,
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config()

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        # Should stop after turn 1 error — never fires the scenario follow-up
        assert adapter.send.call_count == 1
        assert conv.goal_achieved is False
        mock_eval_gen.assert_not_called()

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_max_turns_respected_even_with_scenario_turns(self, mock_eval_gen, mock_get_adapter):
        from stages.s3_execution.conversation_runner import run_conversation

        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=self._make_adapter_response())
        mock_get_adapter.return_value = adapter
        mock_eval_gen.return_value = (None, ConversationState.SATISFIED, None, True, None)

        # 2 scenario turns but max_turns=2 — only turn 1 + 1 scenario turn should run
        scenario_turns = [
            {"turn": 2, "prompt": "Second", "expected_behavior": "exp2"},
            {"turn": 3, "prompt": "Third", "expected_behavior": "exp3"},
        ]
        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First",
            scenario_turns=scenario_turns,
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config(conversation_turns=2)

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        # Should stop at 2 turns max
        assert len(conv.turns) == 2

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_prompt_with_no_scenario_turns_uses_dynamic_generation(self, mock_eval_gen, mock_get_adapter):
        from stages.s3_execution.conversation_runner import run_conversation

        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=self._make_adapter_response())
        mock_get_adapter.return_value = adapter
        # Dynamic gen returns a follow-up (with its own expected_behavior) on first call, then stops
        mock_eval_gen.side_effect = [
            ("Dynamic follow up", ConversationState.CLARIFYING, None, False, "Exp for dynamic turn 2"),
            (None, ConversationState.SATISFIED, None, True, None),
        ]

        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First message",
            scenario_turns=[],  # no pre-crafted turns
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config(conversation_turns=5)

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        # Dynamic generation should have been called at least once
        assert mock_eval_gen.call_count >= 1

        # Second turn query should be the dynamic follow-up
        if len(conv.turns) >= 2:
            assert conv.turns[1].query == "Dynamic follow up"
            # #4: the dynamic turn carries ITS OWN expected_behavior (not turn 1's)
            assert conv.turns[1].expected_behavior == "Exp for dynamic turn 2"

    @patch("stages.s3_execution.conversation_runner._get_adapter")
    @patch("stages.s3_execution.conversation_runner._combined_eval_generate")
    def test_conversation_turns_zero_is_single_turn(self, mock_eval_gen, mock_get_adapter):
        """conversation_turns=0 means 'no multi-turn' — exactly one opening turn, no follow-ups."""
        from stages.s3_execution.conversation_runner import run_conversation

        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=self._make_adapter_response())
        mock_get_adapter.return_value = adapter

        # Even with pre-crafted scenario turns available, none should fire when turns=0.
        prompt = GeneratedPrompt(
            persona_id="p1",
            test_class=TestClass.FUNCTIONAL,
            text="First message",
            scenario_turns=[{"turn": 2, "prompt": "Follow up", "expected_behavior": "exp"}],
        )
        persona = make_persona()
        persona.persona_id = "p1"
        config = make_run_config(conversation_turns=0)

        llm_client = MagicMock()
        conv = self._run(run_conversation(persona, prompt, config, llm_client))

        assert len(conv.turns) == 1            # one opening turn only
        assert adapter.send.call_count == 1    # no follow-up message sent
        mock_eval_gen.assert_not_called()      # no dynamic generation
