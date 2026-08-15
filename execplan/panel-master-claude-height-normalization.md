# Normalize panel-master character stature to Claude

This ExecPlan is a living document. The sections `Progress`,
`Surprises & Discoveries`, `Decision Log`, and `Outcomes & Retrospective`
must be kept current while work proceeds.

## Purpose / Big Picture

The managed `studio-panel-shot` output should place every approved seated
character at approximately Claude's visible head-to-desk height. An operator can
observe the result in DialectiCore's Panel Master review: all six figures have a
consistent stature, still touch the desk correctly, and the panel metadata names
Claude as the reference and records the measured height spread.

## Progress

- [x] (2026-08-15 23:16Z) Inspected the live panel master, its six approved source plates, request provenance, and B1 compositor output geometry.
- [x] (2026-08-15 23:18Z) Identified equal-width fitting in B1 as the cause of 86-117px body-height variance.
- [x] (2026-08-15 23:29Z) Added an explicit stature reference to the DialectiCore-to-B1 request.
- [x] (2026-08-15 23:31Z) Normalized cropped transparent figures to the reference figure height in B1 and added rejecting QC.
- [x] (2026-08-15 23:38Z) Passed 55 DialectiCore media tests and 103 B1 executor/workflow tests.
- [x] (2026-08-15 23:42Z) Dry-ran the compositor against all six real approved plates: every visible body is 99px high with zero spread and semantic QC passes.
- [ ] Deploy the B1 control-plane/CPU compositor and DialectiCore API changes.
- [ ] Regenerate only the panel master, verify the image and geometry, and leave it pending human review.

## Surprises & Discoveries

- Observation: The panel compositor crops transparent figures correctly but then calls `ImageOps.contain` with one common width. Different torso aspect ratios therefore produce different visible heights.
  Evidence: Live `seat_occupancy.body_region.height` values are 0.1472, 0.1375, 0.1264, 0.1472, 0.1625, and 0.1194 (106, 99, 91, 106, 117, and 86px at 720p).
- Observation: Existing semantic QC passed this visibly inconsistent result.
  Evidence: Job `job_56923f857c3d4b90aa0b95494b9f4888` reports `quality_control.status=passed` without a stature-spread measurement.

## Decision Log

- Decision: Make Claude an explicit per-request reference rather than hard-coding Claude into B1.
  Rationale: DialectiCore owns the editorial cast choice; the reusable B1 compositor should validate and honor a supplied participant ID, with a deterministic fallback for older callers.
  Date/Author: 2026-08-15 / Codex
- Decision: Normalize the cropped alpha-bounded figure height, not face height or source canvas height.
  Rationale: Visible seated stature is the user-facing quantity; face/body proportions may legitimately differ, while source canvases include transparent padding.
  Date/Author: 2026-08-15 / Codex

## Outcomes & Retrospective

Implementation is in progress. The original approved seated-character assets
will remain unchanged; only the derived panel master and its dependents may be
regenerated after deployment.

## Context and Orientation

DialectiCore builds the managed request in
`backend/app/services/comfyui_service.py::_b1_studio_panel_payload`. B1 validates
the request in `/opt/b1-ai-hub-source/services/control-plane/app/main.py` and
composes the derived PNG in
`/opt/b1-ai-hub-source/services/control-plane/app/executor.py::compose_studio_panel_image`.
The panel master is a review-gated derived asset; the six approved transparent
seated plates are immutable inputs.

## Plan of Work

Add `stature_reference_participant_id` to the managed request and set it to
`claude` for the current cast. Validate that the value names one participant.
Preprocess each transparent plate, compute the legacy fitted height of the
reference plate, and scale every cropped plate to that height while preserving
aspect ratio. Keep existing desk anchoring and overlap checks. Record reference,
target height, spread, and each scale in output metadata, and fail QC if visible
body heights exceed a small tolerance. Preserve backward compatibility by using
the median legacy fitted height when no reference is supplied.

## Concrete Steps

From `/srv/DialectiCore`, patch the request builder and its test, then run:

    .venv/bin/pytest backend/tests/test_comfyui_service.py -k studio_panel

From `/opt/b1-ai-hub-source`, patch validation, compositing, docs, and tests, then run:

    python -m unittest tests.unit.test_executor tests.unit.test_media_job_workflow_enforcement

Deploy only the affected services using their existing project lifecycle, then
request a replacement panel master through DialectiCore's existing review flow.

## Validation and Acceptance

Unit tests must prove that unequal-aspect transparent figures render to the same
visible height, the reference ID survives input resolution, an unknown reference
is rejected, and older requests remain valid. The live replacement must contain
six visible participants, preserve table and rear screen, show no meaningful
body-height spread, and remain `pending_review`. Approved source-plate IDs and
checksums must be unchanged.

## Idempotence and Recovery

Tests and deployment are repeatable. Do not overwrite approved plates. If live
validation fails, roll back the affected repository commits or restore the prior
service image and retain the existing pending-review panel master. Regeneration
creates a replacement asset through the existing workflow rather than mutating
an immutable artifact.

## Artifacts and Notes

Current panel asset: `2d666cf8-5285-4b05-ad5c-24f293bc5364`.
Current B1 job: `job_56923f857c3d4b90aa0b95494b9f4888`.
Claude source plate: `8e1b7ae2-8c66-476c-ac74-df43ff46a707`.

## Interfaces and Dependencies

The managed media input gains optional string field
`stature_reference_participant_id`. B1's response gains stature normalization
evidence under `studio_panel.quality_control` and per-participant placement
evidence under `studio_panel.seat_occupancy`. Pillow remains the only compositor
dependency.

Plan update 2026-08-15: Created after live evidence isolated equal-width fitting
as the regression source and the user selected Claude as the stature reference.

Plan update 2026-08-15 23:42Z: Recorded the implemented request contract, B1
normalization/QC boundary, passing focused suites, and real-plate dry-run evidence.
