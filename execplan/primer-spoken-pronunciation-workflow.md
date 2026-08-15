# Add a reviewed pronunciation script to primer production

This ExecPlan is a living document. The sections `Progress`, `Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective` must be kept current while implementation proceeds. The repository does not contain a root `PLANS.md`; this plan follows the ExecPlan methodology supplied to Codex.

## Purpose / Big Picture

After this change, an editor can keep the evidence-approved primer wording unchanged while creating a second, explicitly reviewed version optimized for speech synthesis. The system expands or phonetically adapts acronyms, names, numbers, units, and symbols; shows every substitution; permits corrections; plays a short Voicebox sample; and requires approval before narration timing or rendering. A configurable AI may suggest substitutions and punctuation, but cannot silently add, remove, or reorder factual words.

## Progress

- [x] (2026-08-05 05:31Z) Mapped narrator-profile persistence, primer state, visual-plan review, narration timing, Voicebox preview, and render paths.
- [x] (2026-08-05 05:45Z) Added additive pronunciation configuration, spoken-script schemas, and persisted per-episode review state.
- [x] (2026-08-05 05:52Z) Implemented deterministic dictionary, acronym, number, unit, and symbol adaptation; constrained AI suggestions; token-equivalence validation; editing; approval; and audit records.
- [x] (2026-08-05 05:57Z) Bound Voicebox generation and reusable WAV checks to approved spoken text while preserving the canonical script for visual timing.
- [x] (2026-08-05 06:03Z) Added narrator Pronunciation configuration and primer spoken-script comparison, correction, dictionary, preview, save, and approval controls.
- [x] (2026-08-05 06:18Z) Completed focused backend tests, the full primer service suite, frontend tests/build, Ruff, Python compilation, and desktop/mobile browser checks.
- [x] (2026-08-05 06:18Z) Restarted the persistent API and validated live routes plus browser request bodies without running providers or modifying the current episode.

## Surprises & Discoveries

- Observation: `reuse_existing_script` currently also controls reuse of the existing narration WAV, although primer production always keeps the approved visual-plan script.
  Evidence: `PrimerProductionService.produce` passes that flag to `_reusable_narration_asset`.
- Observation: Voicebox transcription QC compares generated audio against the exact text sent to TTS.
  Evidence: `_narrate` calls `verify_spoken_text(... expected_text=script)` and stores `script_sha256` on the audio asset.
- Observation: `Expand numbers` initially affected only the AI prompt, which would make dictionary-only operation misleading.
  Evidence: the deterministic preparation test exposed an unchanged numeric token until pinned `num2words==0.5.14` expansion was added.
- Observation: the primer timeline's intentional wide track enlarged the full document on mobile because the outer one-column grid retained its auto minimum.
  Evidence: browser measurement reported a 753 px document at a 390 px viewport; after constraining grid minima and mobile action wrapping, document width is 390 px and the timeline viewport scrolls internally.
- Observation: a broad two-file backend run completed with 197 passing and 7 failing tests outside this feature, in existing managed-media smoke, system-health, orchestration replay, localization/audio, and timeline-admission expectations.
  Evidence: every new spoken-script API/service test and all 30 primer service tests pass; none of the seven failures exercised the new routes or pronunciation state.

## Decision Log

- Decision: Keep canonical editorial text and spoken TTS text as distinct, checksummed values.
  Rationale: Pronunciation spellings must never alter evidence approval, citations, visual beat narration excerpts, or the published transcript.
  Date/Author: 2026-08-05 / Codex
- Decision: Accept AI output only as explicit source-to-spoken replacements plus punctuation-only formatting whose transformed token sequence exactly matches the server-built candidate.
  Rationale: This permits useful pronunciation assistance while preventing an LLM from silently changing claims, quantities, negations, or sentence order.
  Date/Author: 2026-08-05 / Codex
- Decision: Default pronunciation preparation to disabled for existing profiles and require review when enabled.
  Rationale: Existing episodes remain renderable until an operator opts into the new approval gate.
  Date/Author: 2026-08-05 / Codex
- Decision: Resolve overlapping source strings by longest exact source first and reject only duplicate sources with conflicting spoken values.
  Rationale: valid entries such as `EU-Kommission` and `EU` can coexist deterministically without making ordinary pronunciation dictionaries unusable.
  Date/Author: 2026-08-05 / Codex
- Decision: Pin `num2words` at 0.5.14 for deterministic language-aware integer and decimal expansion.
  Rationale: number pronunciation must continue to work when optional AI assistance is unavailable, and the production images already install project dependencies from `pyproject.toml`.
  Date/Author: 2026-08-05 / Codex

## Outcomes & Retrospective

The reviewed spoken-narration workflow is implemented end to end. Characters now exposes narrator pronunciation policy and dictionary management. Topic primer exposes canonical/spoken comparison, explicit mappings, bounded voice sampling, editing, approval, and stale-state guidance. Enabled profiles cannot generate narration until the current derivative is approved; disabled profiles preserve existing behavior. Live browser fixtures proved the Save and Approve request contracts without provider cost or persistent episode changes, and the current Europe episode remained completed with its existing render. A future production run still needs an operator to enable and save the desired narrator policy, prepare and approve its spoken script, then request fresh narration or a primer re-render.

## Context and Orientation

`backend/app/domain/schemas.py` defines API and persisted payloads. `backend/app/infrastructure/repository.py` stores each narrator profile as JSON, so additive fields with defaults require no database migration. `backend/app/services/primer_production_service.py` stores per-episode primer state under `episode.workflow_control`, creates and approves the visual plan, submits narration to Voicebox, synchronizes measured audio duration, and renders the primer. `backend/app/api/routes.py` exposes these operations. `frontend/src/main.tsx` contains both the narrator editor under Characters and the Topic primer stage. `frontend/src/styles.css` contains the related layout styles.

The canonical script is the evidence-approved text used by the visual plan. The spoken script is a derivative used only as Voicebox input. A profile fingerprint is a SHA-256 hash of pronunciation settings; it makes a previously approved spoken script visibly stale after its narrator pronunciation configuration changes.

## Plan of Work

Add `PrimerPronunciationSettings` and dictionary-entry models to `backend/app/domain/schemas.py`, attach them to `PrimerNarratorProfile`, and add request/status models for preparing, updating, and approving spoken scripts. Store the spoken-script state inside the existing `primer_production` workflow dictionary.

In `PrimerProductionService`, implement deterministic substitutions, configurable custom dictionary handling, an optional OpenAI-compatible JSON suggestion request using the selected narrator or pronunciation endpoint, strict replacement validation, punctuation-only token equivalence, state fingerprinting, edit and approval methods, and audit events. Update narration timing and production to resolve the approved spoken text before Voicebox submission. Keep visual-plan checksums based on canonical text and audio reuse checks based on spoken text.

Add four managed API operations: read via the existing primer status, prepare a spoken script, update its replacement list, and approve it. API failures must be structured HTTP 422 responses and must save episode diagnostics when appropriate.

In `frontend/src/main.tsx`, expose pronunciation controls and an editable dictionary in Primer Narrators. Add a Primer Spoken Narration section after the visual plan with canonical/spoken comparison, editable replacement rows, prepare/refresh, save, approve, and sample controls. Disable narration timing and render while enabled pronunciation is unapproved or stale, and display the required next action in the Topic primer stage status.

## Concrete Steps

From `/srv/DialectiCore`, edit the named backend and frontend files using `apply_patch`. Run:

    .venv/bin/ruff check backend/app backend/tests
    .venv/bin/pytest -q backend/tests/test_primer_production_service.py backend/tests/test_api.py
    cd frontend && npm test && npm run build

Restart only the development API after tests:

    systemctl --user restart dialecticore-api-dev.service
    systemctl --user is-active dialecticore-api-dev.service

Use `playwright-cli` against `http://userver:5173` to verify controls and intercept preparation/approval requests. Use a temporary profile or restore any modified profile payload after the smoke test. Do not start a full Voicebox or render job merely to inspect request wiring.

## Validation and Acceptance

Backend tests must demonstrate that deterministic/custom replacements are visible and stable, malformed AI output falls back safely, unknown or conflicting replacements are rejected, token additions or deletions are rejected, profile or editorial changes mark approval stale, and enabled unapproved pronunciation blocks narration. A successful approved candidate must be the exact text passed to `_narrate`, while visual-plan synchronization still receives the canonical script.

In the browser, Characters must show pronunciation enablement, AI source, policies, and add/remove dictionary entries. Topic primer must show canonical and spoken text plus mappings. Preparing produces review-required state, editing persists server-rebuilt text, approval unlocks narration timing, and a sample button submits only a bounded spoken excerpt. Existing narrator profiles with pronunciation disabled must retain their prior production behavior.

## Idempotence and Recovery

All schema changes are additive JSON defaults and require no destructive migration. Preparing a spoken script replaces only the spoken derivative and archives nothing from the visual plan. Repeating prepare or approval is safe and audited. Disabling pronunciation restores canonical-script narration without deleting prior review evidence. If AI is unavailable or invalid, deterministic and custom dictionary adaptation remains reviewable; no unvalidated AI text is stored.

## Artifacts and Notes

Expected approved state shape:

    {
      "status": "approved",
      "editorial_script_checksum": "...",
      "spoken_script": "Die E U investiert sieben Komma fuenf Milliarden Euro ...",
      "spoken_script_checksum": "...",
      "profile_fingerprint": "...",
      "replacements": [
        {"source": "EU", "spoken": "E U", "category": "acronym", "origin": "deterministic"}
      ]
    }

## Interfaces and Dependencies

No new external package is required. Use Pydantic for structured payload validation, `httpx` and the existing endpoint authentication helpers for optional AI suggestions, Python `re` and `hashlib` for safe transformations and fingerprints, React Query for mutation invalidation, and the existing Voicebox preview endpoint for bounded sample playback.

Revision note (2026-08-05): Initial self-contained plan created after tracing the active primer and narrator paths. The design deliberately makes AI advisory and keeps human approval authoritative.

Revision note (2026-08-05): Implementation completed and validated. Added deterministic number handling and repaired an existing mobile primer overflow discovered during browser acceptance.
