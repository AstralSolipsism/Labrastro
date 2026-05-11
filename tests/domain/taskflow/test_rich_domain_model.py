from __future__ import annotations

from labrastro_server.taskflow.domain.project_state import TraceEntityType
from labrastro_server.taskflow.domain.taskflow_state import (
    AcceptanceExample,
    BriefStatus,
    ComplexityDimensionScore,
    ComplexityEvidenceRecord,
    BriefVersionRecord,
    ComplexityEstimateRecord,
    ConfirmationState,
    DecisionAnswer,
    DecisionOption,
    DecisionRecord,
    DispatchDecisionRecord,
    DispatchDecisionStatus,
    ExampleKind,
    ExampleRecord,
    OpenQuestion,
    QuestionState,
    ReadinessGate,
    ReviewCardAnswer,
    RuleRecord,
    RuleStatus,
    TaskflowEvent,
    TaskflowEventType,
    TaskflowState,
)


def test_taskflow_state_round_trips_rich_discovery_fields() -> None:
    state = TaskflowState.new(
        taskflow_id="taskflow-1",
        project_id="project-1",
        goal_id="goal-1",
        goal_statement="Build complete taskflow discovery state.",
    )
    state.clarification.open_questions.append(
        OpenQuestion(
            id="question-1",
            stage="scope",
            question="Should dispatch require confirmed brief?",
            why_needed="Dispatch boundary affects runtime safety.",
            answer_type="single_choice",
            options=["yes", "no"],
            default_suggestion="yes",
            risk_if_unknown="high",
            state=QuestionState.OPEN,
            priority=1,
            blocking_scope="compile",
            field_bindings=["outputs.confirmed_brief_version"],
            source_refs=["decision-1"],
            blocks_compile=True,
            blocks_dispatch=True,
        )
    )
    state.clarification.rules.append(
        RuleRecord(
            id="rule-1",
            statement="Runtime task creation requires a confirmed brief.",
            status=RuleStatus.CONFIRMED,
            source_decision_id="decision-1",
            example_ids=["example-1"],
            scenario_ids=["scenario-1"],
            risk_level="high",
            confirmation_state=ConfirmationState.CONFIRMED_BY_USER,
            confirmed_by="user",
            confirmed_at="2026-05-11T00:00:00Z",
        )
    )
    state.clarification.examples.append(
        ExampleRecord(
            id="example-1",
            rule_id="rule-1",
            title="Unconfirmed brief blocks dispatch",
            kind=ExampleKind.NEGATIVE,
            given=["A taskflow has a draft brief"],
            when=["Dispatch is requested"],
            then=["The request is rejected"],
            observable_outputs=["runtime_event"],
        )
    )
    state.clarification.acceptance_examples.append(
        AcceptanceExample(
            id="acceptance-1",
            title="Confirmed brief gates dispatch",
            rule_id="rule-1",
            kind=ExampleKind.POSITIVE,
            given=["Brief v1 is confirmed"],
            when=["The plan is compiled"],
            then=["Work items contain acceptance metadata"],
            definition_of_done=["Trace links include the rule"],
            test_suggestions=["service-level compile test"],
            observable_outputs=["tests", "trace_link"],
            gherkin_text="Scenario: confirmed brief gates dispatch",
            source_brief_version=1,
        )
    )
    state.design.local_decisions.append(
        DecisionRecord(
            id="decision-1",
            decision_type="architecture",
            question="How deep should confirmation be?",
            topic="Confirmation boundary",
            why_it_matters="It separates planning from execution.",
            options=[
                DecisionOption(
                    id="brief",
                    label="Brief confirmation",
                    tradeoff="Simple and safe for the first execution boundary.",
                    impact={"backend": "requires brief versions"},
                )
            ],
            recommended="brief",
            recommendation_rationale="Brief versioning keeps the boundary auditable.",
            chosen="brief",
            state=ConfirmationState.CONFIRMED_BY_USER,
            linked_rule_ids=["rule-1"],
        )
    )
    state.outputs.brief_versions.append(
        BriefVersionRecord(
            id="brief-1",
            version=1,
            status=BriefStatus.CONFIRMED,
            goal={"id": "goal-1"},
            rules=[state.clarification.rules[0].to_dict()],
            examples=[state.clarification.examples[0].to_dict()],
            acceptance_examples=[state.clarification.acceptance_examples[0].to_dict()],
            scenarios=[{"id": "scenario-1", "title": "Confirmed brief scenario"}],
            decisions=[state.design.local_decisions[0].to_dict()],
            work_item_candidates=[
                {
                    "id": "candidate-1",
                    "title": "Implement confirmed brief gate",
                    "description": "Work item from immutable brief snapshot.",
                    "acceptance_refs": ["acceptance-1"],
                    "scenario_refs": ["scenario-1"],
                }
            ],
            content_hash="hash-1",
            previous_version=None,
            causation_event_id="event-1",
            source_event_ids=["event-1"],
            diff_summary={"changed_sections": ["rules"]},
            confirmed_by="user",
            confirmed_at="2026-05-11T00:00:00Z",
        )
    )
    state.outputs.current_brief_version = 1
    state.outputs.confirmed_brief_version = 1
    state.outputs.review_card_answers.append(
        ReviewCardAnswer(
            id="review-answer-1",
            card_id="taskflow-1:decision:decision-1",
            action="accept_recommendation",
            value="brief",
            actor="user",
            metadata={"brief_version": 1},
        )
    )
    state.outputs.dispatch_decisions.append(
        DispatchDecisionRecord(
            id="dispatch-decision-1",
            brief_version=1,
            work_item_ids=["work-candidate-1"],
            status=DispatchDecisionStatus.CONFIRMED,
            requested_by="user",
            confirmed_by="user",
            confirmed_at="2026-05-11T00:00:00Z",
            readiness_snapshot=[{"id": "gate-confirmed-brief", "passed": True}],
        )
    )
    state.compiler.complexity_estimate = ComplexityEstimateRecord(
        level="L2",
        score=14,
        recipe_id="standard-feature",
        dimension_scores={"goal_clarity": 1, "technical_risk": 2},
        signal_evidence={"technical_risk": ["high-risk rule"]},
        required_steps=["scenarios", "acceptance_examples"],
        completed_steps=["scenarios"],
        required_artifacts=["brief"],
        rationale="Risk requires standard feature discovery.",
    )
    state.events.append(
        TaskflowEvent(
            id="event-1",
            type=TaskflowEventType.BRIEF_CONFIRMED,
            taskflow_id="taskflow-1",
            project_id="project-1",
            goal_id="goal-1",
            actor="user",
            payload={"brief_version": 1},
        )
    )

    restored = TaskflowState.from_dict(state.to_dict())

    assert restored.clarification.open_questions[0].blocking_scope == "compile"
    assert restored.clarification.rules[0].status == RuleStatus.CONFIRMED
    assert restored.clarification.examples[0].kind == ExampleKind.NEGATIVE
    assert restored.clarification.acceptance_examples[0].rule_id == "rule-1"
    assert restored.design.local_decisions[0].options[0].impact["backend"]
    assert restored.outputs.brief_versions[0].status == BriefStatus.CONFIRMED
    assert restored.outputs.brief_versions[0].rules[0]["statement"].startswith("Runtime")
    assert restored.outputs.brief_versions[0].content_hash == "hash-1"
    assert restored.outputs.review_card_answers[0].card_id.endswith("decision-1")
    assert restored.outputs.dispatch_decisions[0].status == DispatchDecisionStatus.CONFIRMED
    assert restored.compiler.complexity_estimate is not None
    assert restored.events[0].type == TaskflowEventType.BRIEF_CONFIRMED


def test_complexity_assessment_fields_round_trip() -> None:
    evidence = ComplexityEvidenceRecord(
        id="evidence-plugin",
        dimension="interface_impact",
        source_type="goal",
        source_id="goal-1",
        source_path="intent.goal_statement",
        score_delta=1,
        confidence=0.6,
        rationale="Plugin integration touches an external framework contract.",
        brief_version=1,
        extracted_by="agent",
        metadata={"token": "plugin"},
    )
    detail = ComplexityDimensionScore(
        dimension="interface_impact",
        score=1,
        evidence_ids=["evidence-plugin"],
        rationale="External plugin integration.",
    )
    estimate = ComplexityEstimateRecord(
        level="L1",
        score=6,
        recipe_id="small-task",
        dimension_scores={"interface_impact": 1},
        evidence=[evidence],
        dimension_details=[detail],
        level_floor="L1",
        hard_escalations=["external-integration-floor"],
        recipe_policy_id="recipe-policy.v1.small-task",
        required_gates=["gate-acceptance-contract"],
        compile_blockers=["gate-acceptance-contract"],
        dispatch_blockers=["gate-dispatch-confirmation"],
        required_steps=["goal", "scope", "acceptance"],
        required_artifacts=["brief", "scope", "acceptance"],
    )
    restored = ComplexityEstimateRecord.from_dict(estimate.to_dict())

    assert restored.evidence[0].dimension == "interface_impact"
    assert restored.evidence[0].metadata["token"] == "plugin"
    assert restored.dimension_details[0].evidence_ids == ["evidence-plugin"]
    assert restored.level_floor == "L1"
    assert restored.hard_escalations == ["external-integration-floor"]
    assert restored.compile_blockers == ["gate-acceptance-contract"]


def test_readiness_gate_round_trips_phase_and_evidence_refs() -> None:
    gate = ReadinessGate(
        id="gate-artifact-api_contract",
        name="api-contract",
        passed=False,
        rationale="API contract is required.",
        severity="high",
        phase="compile",
        source="complexity",
        evidence_ids=["evidence-public-api"],
        artifact_keys=["api_contract"],
    )

    restored = ReadinessGate.from_dict(gate.to_dict())

    assert restored.phase == "compile"
    assert restored.source == "complexity"
    assert restored.evidence_ids == ["evidence-public-api"]
    assert restored.artifact_keys == ["api_contract"]
    assert restored.blocks_compile is True
    assert restored.blocks_dispatch is False


def test_trace_entity_type_covers_discovery_and_brief_nodes() -> None:
    assert TraceEntityType.BRIEF.value == "brief"
    assert TraceEntityType.RULE.value == "rule"
    assert TraceEntityType.EXAMPLE.value == "example"
    assert TraceEntityType.SCENARIO.value == "scenario"
    assert TraceEntityType.OPEN_QUESTION.value == "open_question"
    assert TraceEntityType.DECISION_ANSWER.value == "decision_answer"
    assert TraceEntityType.DISPATCH_DECISION.value == "dispatch_decision"


def test_decision_answer_round_trips_user_authority_metadata() -> None:
    answer = DecisionAnswer(
        id="answer-1",
        decision_id="decision-1",
        selected_option_id="brief",
        answer="brief",
        rationale="This keeps dispatch explicit.",
        actor="user",
        source="api",
        source_brief_version=1,
    )

    restored = DecisionAnswer.from_dict(answer.to_dict())

    assert restored.decision_id == "decision-1"
    assert restored.selected_option_id == "brief"
    assert restored.source_brief_version == 1


def test_dispatch_decision_round_trips_execution_confirmation_metadata() -> None:
    decision = DispatchDecisionRecord(
        id="dispatch-decision-1",
        brief_version=3,
        work_item_ids=["work-1", "work-2"],
        status=DispatchDecisionStatus.REQUESTED,
        requested_by="agent",
        rationale="Ready for dispatch review.",
        readiness_snapshot=[
            {"id": "gate-acceptance-coverage", "name": "acceptance-coverage", "passed": True}
        ],
        metadata={"plan_id": "plan-1"},
    )

    restored = DispatchDecisionRecord.from_dict(decision.to_dict())

    assert restored.brief_version == 3
    assert restored.work_item_ids == ["work-1", "work-2"]
    assert restored.status == DispatchDecisionStatus.REQUESTED
    assert restored.readiness_snapshot[0]["id"] == "gate-acceptance-coverage"
