# Discussion Engine

The discussion engine runs one accepted turn at a time. Fixed editorial turns
cover the host opening, opening positions, host challenge prompts, and final
synthesis; panelist response turns are selected by
`deterministic_discussion_controller_v1` instead of a static round-robin list.

Inputs to each participant:

- Shared evidence pack summary.
- Public transcript state.
- Current phase.
- Remaining duration.
- Latest host instruction.
- That participant's private memory.
- Permitted tool results, when the participant's configured tool policy allows
  them.

The engine does not expose other participants' private memory. Every accepted
turn records its discussion session ID, structured output, raw provider
response, generation metadata, speaker balance, and audit events. Each
participant memory also has its own ID and discussion session ID, so persisted
episode aggregates expose the required `DiscussionTurn` and `ParticipantMemory`
entity links directly rather than only through array nesting.

Participant tool use is policy-scoped. Profiles with `tool_policy_id` set to
`evidence_pack_lookup`, `evidence_lookup`, `source_grounded_tools`, or
`research_tools` receive deterministic source-bound evidence lookup results from
the episode evidence pack before their turn is generated. Profiles with
`no_tools` receive no tool output. The initial implementation does not browse the
web or call remote tools during discussion; it searches only approved episode
evidence and returns bounded claim/source snippets for the prompt's
`{tool_results}` field.

Every turn stores `discussion_tool_usage.v1` in
`generation_metadata.tool_usage`, including policy ID, call count, result count,
configured time/result limits, and per-call status. `discussion.tools.used` audit
events are written for turns and regenerated turns that used a tool.
`discussion_session.controller_state` preserves a bounded `tool_usage_log` plus
aggregate `tool_call_count` and `tool_result_count` for operator review and
restartability.

The model gateway currently executes deterministic mock, OpenAI-compatible,
Ollama, Anthropic-compatible, Mistral-compatible, and generic HTTP adapters
through one `ModelClient` protocol. Provider responses must validate as
`StructuredTurnOutput` before the turn is accepted. If a provider returns
malformed structured output, the gateway retries that same participant turn once
with a correction instruction and records `structured_output_retry.v1` metadata
on the accepted turn when the retry succeeds.

Discussion prompts are rendered from versioned prompt templates seeded from
`examples/prompt-templates.json` and persisted in
`discussion_prompt_template_records`. Each template records a template ID,
semantic version, variable set, creator, creation timestamp, enabled flag, and
change summary, with API and Web UI administration. Participant profiles may
only reference enabled templates whose participant type matches the profile, and
templates already referenced by profiles cannot be disabled or retyped. Every
generated discussion turn records the selected `prompt_template_id`,
`prompt_template_version`, and safe template audit fields in generation metadata
alongside provider, model, and sampling settings.

Episode definitions reference participant profile IDs. At creation time the API
loads the persisted profiles in the configured assignment order, so one host and
the selected panelists keep their own endpoint, model, prompt, perspective,
sampling settings, voice reference, visual reference, and private memory.
Before the first turn is generated, the engine now validates every enabled
participant's model configuration. Startup is rejected if a character has an
empty model ID, points at an unknown model endpoint, or points at a disabled
endpoint, so the discussion cannot begin with a partially configured cast.
Workflow-worker summaries expose the same gate as
`discussion_model_configuration.v1` with `model_configuration_blocked` counts
and the affected participant IDs.

For dynamic panelist turns, the controller scores eligible speakers using the
current host instruction, topic dimensions, expertise/perspective overlap,
pending questions addressed to a participant, unresolved disagreement pressure,
speaker balance, recent-speaker penalty, repetition risk, and remaining
duration. The selected turn stores `speaker_selection.v1` in
`generation_metadata.speaker_selection`, including candidate scores, score
components, addressed pending-question IDs, and the deterministic selection
reason. `discussion_session.controller_state` preserves pending and answered
questions plus the latest candidate-score evidence for operator review and
restartability.

Each selected turn also receives a subject-neutral `discussion_turn_contract.v1`.
It states the contribution shape for the current editorial turn, a generation-time
word budget, the episode source language, and any direct question that the
controller selected the participant to answer. Panel-wide questions can be routed
to an eligible speaker as well as questions addressed by ID. The contract is
persisted in `generation_metadata.turn_contract`; a routed answer is linked to
the source turn even when the provider omits the optional `responding_to` field.

The controller reserves duration for every remaining required turn, including
closing positions and the host synthesis. This prevents early turns from using
the time required to conclude the episode. If a provider still exceeds its
budget, duration control first keeps the last complete sentence inside the
allowed duration and records its truncation strategy. The
`discussion_conversation_quality` result reports host synthesis presence,
post-generation duration adjustments, routed-question links, and open direct
questions independently from the minimum-structure QC.

During transcript review, a producer can regenerate one discussion turn through
the same participant model or exclude one turn from the broadcast transcript.
Both actions are blocked after approval and create a new broadcast transcript
version instead of silently changing the approved artifact.

Every broadcast transcript version receives a deterministic semantic-fidelity QC
result. Approval is blocked only for failing QC; warnings, such as intentional
turn exclusions, remain reviewable by the producer.

Duration is controlled before each turn is accepted. The controller computes the
remaining episode budget, divides it across the remaining planned turns, caps the
allowance by `maximum_monologue_seconds`, and deterministically shortens
overlong responses while recording duration-control metadata. The discussion
duration QC result verifies the total estimated runtime stays within the
configured maximum and that accepted turns do not exceed the monologue limit.
