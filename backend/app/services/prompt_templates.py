from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.domain.schemas import (
    DiscussionPromptTemplate as DiscussionPromptTemplateModel,
)
from app.domain.schemas import ParticipantMemory, ParticipantProfile


@dataclass(frozen=True)
class PromptTemplate:
    id: str
    version: str
    participant_type: str
    system: str
    user: str
    variables: dict[str, Any]
    created_by: str
    created_at: str
    enabled: bool
    change_summary: str

    def metadata(self) -> dict[str, Any]:
        return {
            "prompt_template_id": self.id,
            "prompt_template_version": self.version,
            "prompt_template_created_by": self.created_by,
            "prompt_template_created_at": self.created_at,
            "prompt_template_change_summary": self.change_summary,
        }


@dataclass(frozen=True)
class RenderedPrompt:
    messages: list[dict[str, str]]
    template: PromptTemplate

    def metadata(self) -> dict[str, Any]:
        return self.template.metadata()


class PromptTemplateRegistry:
    def __init__(
        self,
        template_path: Path | None = None,
        templates: list[DiscussionPromptTemplateModel] | None = None,
        template_provider: Callable[[], list[DiscussionPromptTemplateModel]] | None = None,
    ) -> None:
        self.template_path = template_path or (
            Path(__file__).resolve().parents[3] / "examples" / "prompt-templates.json"
        )
        self._template_provider = template_provider
        self._templates = (
            self._templates_from_models(templates)
            if templates is not None
            else self._load_templates(self.template_path)
        )

    def get(self, template_id: str) -> PromptTemplate:
        templates = self._current_templates()
        template = templates.get(template_id)
        if template and template.enabled:
            return template
        fallback_id = (
            "moderator_v1"
            if "moderator" in template_id or "host" in template_id
            else "panelist_v1"
        )
        fallback = templates[fallback_id]
        return PromptTemplate(
            id=template_id,
            version="unregistered",
            participant_type=fallback.participant_type,
            system=fallback.system,
            user=fallback.user,
            variables=fallback.variables,
            created_by="dialecticore",
            created_at="unregistered",
            enabled=True,
            change_summary=f"Fallback rendering for unregistered template {template_id}.",
        )

    def render(self, participant: ParticipantProfile, context: Any) -> RenderedPrompt:
        template = self.get(participant.system_prompt_template)
        values = prompt_values(participant, context)
        return RenderedPrompt(
            messages=[
                {"role": "system", "content": template.system.format_map(values)},
                {"role": "user", "content": template.user.format_map(values)},
            ],
            template=template,
        )

    def _load_templates(self, path: Path) -> dict[str, PromptTemplate]:
        payload = json.loads(path.read_text(encoding="utf-8"))
        shared_variables = payload.get("variables", {})
        templates: dict[str, PromptTemplate] = {}
        for raw_template in payload.get("templates", []):
            template = PromptTemplate(
                id=raw_template["id"],
                version=str(raw_template["version"]),
                participant_type=raw_template["participant_type"],
                system=raw_template["system"],
                user=raw_template["user"],
                variables=raw_template.get("variables", shared_variables),
                created_by=raw_template["created_by"],
                created_at=raw_template["created_at"],
                enabled=bool(raw_template.get("enabled", True)),
                change_summary=raw_template["change_summary"],
            )
            templates[template.id] = template
        return templates

    def _current_templates(self) -> dict[str, PromptTemplate]:
        if self._template_provider is None:
            return self._templates
        templates = self._templates_from_models(self._template_provider())
        if "moderator_v1" not in templates or "panelist_v1" not in templates:
            defaults = self._load_templates(self.template_path)
            return {**defaults, **templates}
        return templates

    def _templates_from_models(
        self,
        templates: list[DiscussionPromptTemplateModel] | None,
    ) -> dict[str, PromptTemplate]:
        if not templates:
            return {}
        return {
            template.id: PromptTemplate(
                id=template.id,
                version=template.version,
                participant_type=template.participant_type.value,
                system=template.system,
                user=template.user,
                variables=template.variables,
                created_by=template.created_by,
                created_at=template.created_at.isoformat(),
                enabled=template.enabled,
                change_summary=template.change_summary,
            )
            for template in templates
        }


def prompt_values(participant: ParticipantProfile, context: Any) -> dict[str, str]:
    public_transcript = "\n".join(context.public_transcript[-8:]) or "(no prior turns)"
    private_memory = _private_memory_json(context.private_memory)
    return {
        "display_name": participant.display_name,
        "perspective": participant.perspective,
        "expertise": participant.expertise,
        "speaking_style": participant.speaking_style,
        "central_question": context.central_question,
        "phase": context.phase,
        "discussion_intensity": getattr(context, "discussion_intensity", "medium"),
        "latest_host_instruction": context.latest_host_instruction,
        "remaining_seconds": f"{context.remaining_seconds:.1f}",
        "required_dimensions": ", ".join(context.required_dimensions),
        "evidence_summary": "\n".join(context.evidence_summary) or "(none)",
        "available_evidence_refs": ", ".join(context.available_evidence_refs) or "(none)",
        "tool_results": _tool_results_json(getattr(context, "tool_results", [])),
        "public_transcript": public_transcript,
        "private_memory": private_memory,
    }


def _private_memory_json(private_memory: ParticipantMemory) -> str:
    return json.dumps(private_memory.model_dump(mode="json"), sort_keys=True)


def _tool_results_json(tool_results: list[dict[str, Any]]) -> str:
    if not tool_results:
        return "(none)"
    return json.dumps(tool_results, sort_keys=True)
