"""Internal staged state for capability package ingestion.

This module owns capability-package business assembly. AgentRun remains a
runtime/event source; the service layer turns its output into field patches and
uses this assembler to produce a compatible draft.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CapabilityFailureCode(str, Enum):
    SOURCE_INPUT_INVALID = "source_input_invalid"
    SOURCE_DISCOVERY_INCOMPLETE = "source_discovery_incomplete"
    SOURCE_DISCOVERY_PARTIAL = "source_discovery_partial"
    FIELD_GENERATION_INCOMPLETE = "field_generation_incomplete"
    MODEL_OUTPUT_INCOMPLETE = "model_output_incomplete"
    DRAFT_GENERATION_INTERRUPTED = "draft_generation_interrupted"
    DRAFT_FIELD_MISSING = "draft_field_missing"
    DRAFT_VALIDATION_FAILED = "draft_validation_failed"
    INSTALL_FAILED = "install_failed"


@dataclass(frozen=True)
class CapabilityDraftFieldPatch:
    field_path: str
    value: Any
    source_refs: list[dict[str, Any]] = field(default_factory=list)
    confidence: float | None = None
    diagnostics: list[str] = field(default_factory=list)
    producer_event_refs: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_dict(
        cls,
        value: dict[str, Any],
        *,
        producer_event_refs: list[dict[str, Any]] | None = None,
    ) -> "CapabilityDraftFieldPatch | None":
        field_path = str(value.get("field_path") or value.get("path") or "").strip()
        if not field_path:
            return None
        refs = _dict_list(value.get("producer_event_refs"))
        if producer_event_refs:
            refs = _dedupe_refs([*refs, *producer_event_refs])
        confidence = value.get("confidence")
        return cls(
            field_path=field_path,
            value=deepcopy(value.get("value")),
            source_refs=_source_ref_list(value.get("source_refs")),
            confidence=float(confidence) if isinstance(confidence, (int, float)) else None,
            diagnostics=[str(item) for item in value.get("diagnostics", [])]
            if isinstance(value.get("diagnostics"), list)
            else [],
            producer_event_refs=refs,
        )

    @classmethod
    def from_legacy_full_draft(
        cls,
        draft: dict[str, Any],
        *,
        producer_event_refs: list[dict[str, Any]] | None = None,
    ) -> "CapabilityDraftFieldPatch":
        return cls(
            field_path="full_draft",
            value=deepcopy(draft),
            producer_event_refs=list(producer_event_refs or []),
        )

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "field_path": self.field_path,
            "value": deepcopy(self.value),
        }
        if self.source_refs:
            result["source_refs"] = [dict(item) for item in self.source_refs]
        if self.confidence is not None:
            result["confidence"] = self.confidence
        if self.diagnostics:
            result["diagnostics"] = list(self.diagnostics)
        if self.producer_event_refs:
            result["producer_event_refs"] = [
                dict(item) for item in self.producer_event_refs
            ]
        return result


@dataclass(frozen=True)
class CapabilitySourceEvidence:
    source_bundle: dict[str, Any]
    source_bundle_artifact_id: str = ""
    seed_source_bundle_artifact_id: str = ""

    @property
    def source(self) -> dict[str, Any]:
        source = self.source_bundle.get("source")
        return dict(source) if isinstance(source, dict) else {}


@dataclass(frozen=True)
class CapabilityDraftAssemblyResult:
    draft: dict[str, Any] | None
    field_state: dict[str, dict[str, Any]]
    missing_fields: list[str]
    failure_code: CapabilityFailureCode | str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "draft_present": isinstance(self.draft, dict),
            "field_state": deepcopy(self.field_state),
            "missing_fields": list(self.missing_fields),
            "failure_code": (
                self.failure_code.value
                if isinstance(self.failure_code, CapabilityFailureCode)
                else str(self.failure_code or "")
            ),
        }


@dataclass(frozen=True)
class CapabilityIngestState:
    phase: str
    agent_run_id: str
    source_evidence_state: dict[str, Any] = field(default_factory=dict)
    field_generation_state: dict[str, Any] = field(default_factory=dict)
    draft_assembly_state: dict[str, Any] = field(default_factory=dict)
    validation_state: dict[str, Any] = field(default_factory=dict)
    failure: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "phase": self.phase,
            "agent_run_id": self.agent_run_id,
            "source_evidence_state": deepcopy(self.source_evidence_state),
            "field_generation_state": deepcopy(self.field_generation_state),
            "draft_assembly_state": deepcopy(self.draft_assembly_state),
            "validation_state": deepcopy(self.validation_state),
        }
        if self.failure is not None:
            result["failure"] = deepcopy(self.failure)
        return result


class CapabilityDraftAssembler:
    required_fields: tuple[str, ...] = (
        "id",
        "name",
        "contributions",
        "install_plan",
        "usage",
        "evidence",
        "risk_level",
    )

    def assemble(
        self,
        *,
        source_bundle: dict[str, Any] | None,
        patches: list[CapabilityDraftFieldPatch],
    ) -> CapabilityDraftAssemblyResult:
        field_state = _field_state_from_patches(patches)
        full_draft = _last_patch_value(patches, "full_draft")
        if isinstance(full_draft, dict):
            return CapabilityDraftAssemblyResult(
                draft=deepcopy(full_draft),
                field_state=field_state,
                missing_fields=[],
            )

        draft: dict[str, Any] = {}
        bundle = source_bundle if isinstance(source_bundle, dict) else {}
        source = bundle.get("source")
        if isinstance(source, dict):
            draft["source"] = deepcopy(source)
        for patch in patches:
            if patch.field_path == "full_draft":
                continue
            if not _is_draft_field_path(patch.field_path):
                continue
            _set_draft_value(draft, patch.field_path, patch.value)

        missing = [field for field in self.required_fields if _field_missing(draft, field)]
        if missing:
            failure_code = (
                CapabilityFailureCode.DRAFT_FIELD_MISSING
                if _has_draft_field_patch(patches)
                else CapabilityFailureCode.FIELD_GENERATION_INCOMPLETE
            )
            return CapabilityDraftAssemblyResult(
                draft=None,
                field_state=field_state,
                missing_fields=missing,
                failure_code=failure_code,
            )
        return CapabilityDraftAssemblyResult(
            draft=draft,
            field_state=field_state,
            missing_fields=[],
        )


def extract_capability_draft_field_patches(
    value: Any,
    *,
    producer_event_refs: list[dict[str, Any]] | None = None,
) -> list[CapabilityDraftFieldPatch]:
    patches: list[CapabilityDraftFieldPatch] = []
    for parsed in _json_values_from_text(str(value or "")):
        patches.extend(
            _capability_draft_field_patches_from_json_value(
                parsed,
                producer_event_refs=producer_event_refs,
            )
        )
    return patches


def _capability_draft_field_patches_from_json_value(
    parsed: Any,
    *,
    producer_event_refs: list[dict[str, Any]] | None = None,
) -> list[CapabilityDraftFieldPatch]:
    if isinstance(parsed, list):
        patches: list[CapabilityDraftFieldPatch] = []
        for item in parsed:
            patches.extend(
                _capability_draft_field_patches_from_json_value(
                    item,
                    producer_event_refs=producer_event_refs,
                )
            )
        return patches
    if not isinstance(parsed, dict):
        return []
    patch_payload = parsed.get("capability_draft_patch")
    if isinstance(patch_payload, dict):
        patch = CapabilityDraftFieldPatch.from_dict(
            patch_payload,
            producer_event_refs=producer_event_refs,
        )
        return [patch] if patch is not None else []
    patch_list = parsed.get("capability_draft_patches")
    if isinstance(patch_list, list):
        patches: list[CapabilityDraftFieldPatch] = []
        for item in patch_list:
            if not isinstance(item, dict):
                continue
            patch = CapabilityDraftFieldPatch.from_dict(
                item,
                producer_event_refs=producer_event_refs,
            )
            if patch is not None:
                patches.append(patch)
        return patches
    if isinstance(parsed.get("contributions"), dict) or isinstance(parsed.get("components"), list):
        return [
            CapabilityDraftFieldPatch.from_legacy_full_draft(
                parsed,
                producer_event_refs=producer_event_refs,
            )
        ]
    return []


def _field_state_from_patches(
    patches: list[CapabilityDraftFieldPatch],
) -> dict[str, dict[str, Any]]:
    state: dict[str, dict[str, Any]] = {}
    for patch in patches:
        entry: dict[str, Any] = {
            "status": "filled",
            "value": deepcopy(patch.value),
        }
        if patch.source_refs:
            entry["source_refs"] = [dict(item) for item in patch.source_refs]
        if patch.confidence is not None:
            entry["confidence"] = patch.confidence
        if patch.diagnostics:
            entry["diagnostics"] = list(patch.diagnostics)
        if patch.producer_event_refs:
            entry["producer_event_refs"] = [
                dict(item) for item in patch.producer_event_refs
            ]
        state[patch.field_path] = entry
    return state


def _last_patch_value(
    patches: list[CapabilityDraftFieldPatch],
    field_path: str,
) -> Any:
    for patch in reversed(patches):
        if patch.field_path == field_path:
            return patch.value
    return None


def _is_draft_field_path(field_path: str) -> bool:
    root = str(field_path or "").split(".", 1)[0]
    return root in {
        "id",
        "name",
        "description",
        "source",
        "source_inventory",
        "components",
        "contributions",
        "install_plan",
        "usage",
        "effective_capabilities",
        "evidence",
        "credentials",
        "risk_level",
        "execution_policy",
        "notes",
        "runtime_footprint",
        "hooks",
        "materialization_plan",
    }


def _has_draft_field_patch(patches: list[CapabilityDraftFieldPatch]) -> bool:
    return any(_is_draft_field_path(patch.field_path) for patch in patches)


def _set_draft_value(draft: dict[str, Any], field_path: str, value: Any) -> None:
    parts = [part for part in str(field_path or "").split(".") if part]
    if not parts:
        return
    cursor = draft
    for part in parts[:-1]:
        nested = cursor.get(part)
        if not isinstance(nested, dict):
            nested = {}
            cursor[part] = nested
        cursor = nested
    cursor[parts[-1]] = deepcopy(value)


def _field_missing(draft: dict[str, Any], field_path: str) -> bool:
    value, found = _draft_field_value(draft, field_path)
    if not found:
        return True
    if value is None:
        return True
    if field_path in {"install_plan", "usage"}:
        return not isinstance(value, list)
    if field_path in {"id", "name", "risk_level"}:
        return not str(value).strip()
    if field_path == "contributions":
        return not _contributions_have_components(value)
    if field_path == "evidence":
        return not isinstance(value, list) or not value
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)) and not value:
        return True
    return False


def _draft_field_value(draft: dict[str, Any], field_path: str) -> tuple[Any, bool]:
    value: Any = draft
    for part in field_path.split("."):
        if not isinstance(value, dict) or part not in value:
            return None, False
        value = value[part]
    return value, True


def _contributions_have_components(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    return any(isinstance(item, list) and bool(item) for item in value.values())


def _dict_list(value: Any) -> list[dict[str, Any]]:
    return [dict(item) for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def _source_ref_list(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    values = value if isinstance(value, list) else [value]
    refs = [_source_ref_dict(item) for item in values]
    return _dedupe_refs([item for item in refs if item])


def _source_ref_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    text = str(value or "").strip()
    if not text:
        return {}
    lowered = text.lower()
    if lowered.startswith("cap-src-doc-"):
        return {"source_document_id": text}
    if "://" in text:
        return {"url": text}
    if _source_ref_looks_like_path(text):
        return {"source_path": text}
    return {"content_ref": text}


def _source_ref_looks_like_path(value: str) -> bool:
    text = str(value or "").strip().replace("\\", "/")
    if "/" in text:
        return True
    lowered = text.lower()
    return lowered.endswith(
        (
            ".md",
            ".mdx",
            ".txt",
            ".json",
            ".yaml",
            ".yml",
            ".toml",
            "skill.md",
        )
    )


def _dedupe_refs(refs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for ref in refs:
        key = json.dumps(ref, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(dict(ref))
    return result


_JSON_DECODER = json.JSONDecoder()


def _json_values_from_text(value: str) -> list[Any]:
    text = str(value or "")
    values: list[Any] = []
    index = 0
    while index < len(text):
        next_object = text.find("{", index)
        next_array = text.find("[", index)
        starts = [item for item in (next_object, next_array) if item >= 0]
        if not starts:
            break
        index = min(starts)
        try:
            parsed, end = _JSON_DECODER.raw_decode(text, index)
        except json.JSONDecodeError:
            index += 1
            continue
        values.append(parsed)
        index = max(end, index + 1)
    return values
