import { describe, expect, it } from "vitest";
import {
  manualEditEvidenceSummary,
  productionHandoffBlockerSummary,
  workflowAdmissionStatus,
  workflowAdmissionSummaryFromAdvance,
  workflowAdmissionSummaryText,
  workflowNextActionSummary,
  workflowRunStartStatus,
  workflowRunStartSummaryFromAdvance,
  workflowRunStartSummaryText,
} from "./workflowAdvanceEvidence";

describe("workflow advance evidence", () => {
  it("extracts selected-episode run-start evidence", () => {
    const summary = workflowRunStartSummaryFromAdvance(
      {
        episode: { id: "episode-1" },
        summary: {
          workflow_run_starts: {
            schema_version: "workflow_run_start_summary.v1",
            episodes_scanned: 1,
            workflow_runs_started: 1,
            skipped: 0,
            error_count: 0,
          },
        },
      },
      "episode-1"
    );

    expect(summary).toMatchObject({
      schema_version: "workflow_run_start_summary.v1",
      workflow_runs_started: 1,
    });
    expect(workflowRunStartStatus(summary)).toBe("durable run started");
    expect(workflowRunStartSummaryText(summary)).toBe(
      "1 started · 1 scanned · 0 skipped · 0 errors"
    );
  });

  it("ignores stale advance evidence for another episode", () => {
    expect(
      workflowRunStartSummaryFromAdvance(
        {
          episode: { id: "episode-2" },
          summary: {
            workflow_run_starts: {
              episodes_scanned: 1,
              workflow_runs_started: 1,
            },
          },
        },
        "episode-1"
      )
    ).toBeNull();
  });

  it("extracts and summarizes stage admission evidence", () => {
    const summary = workflowAdmissionSummaryFromAdvance(
      {
        episode: { id: "episode-1" },
        summary: {
          workflow_admission: {
            schema_version: "workflow_stage_admission_summary.v1",
            episodes_scanned: 1,
            active_run_episode_count: 0,
            missing_run_episode_count: 1,
            blocked_episode_count: 0,
            missing_run_episode_ids: ["episode-secret"],
          },
        },
      },
      "episode-1"
    );

    expect(summary).toMatchObject({
      schema_version: "workflow_stage_admission_summary.v1",
      missing_run_episode_count: 1,
    });
    expect(workflowAdmissionStatus(summary)).toBe("start durable run first");
    expect(workflowAdmissionSummaryText(summary)).toBe(
      "0 active · 1 missing run · 0 blocked · 1 scanned"
    );
    expect(workflowAdmissionSummaryText(summary)).not.toContain("episode-secret");
  });

  it("labels active workflow stage admission", () => {
    const summary = {
      episodes_scanned: 1,
      active_run_episode_count: 1,
      missing_run_episode_count: 0,
      blocked_episode_count: 0,
    };

    expect(workflowAdmissionStatus(summary)).toBe("stage execution admitted");
    expect(workflowAdmissionSummaryText(summary)).toBe(
      "1 active · 0 missing run · 0 blocked · 1 scanned"
    );
  });

  it("summarizes skipped and error states without exposing nested payloads", () => {
    expect(
      workflowRunStartStatus({
        episodes_scanned: 1,
        workflow_runs_started: 0,
        skipped: 1,
        error_count: 0,
      })
    ).toBe("existing run or gated state");

    const failing = {
      episodes_scanned: 1,
      workflow_runs_started: 0,
      skipped: 0,
      error_count: 1,
      errors: [{ episode_id: "episode-secret", error: "episode workflow is paused" }],
    };
    expect(workflowRunStartStatus(failing)).toBe("run start error");
    expect(workflowRunStartSummaryText(failing)).toBe(
      "0 started · 1 scanned · 0 skipped · 1 errors"
    );
    expect(workflowRunStartSummaryText(failing)).not.toContain("episode-secret");
  });

  it("summarizes manual edit evidence without exposing raw event details", () => {
    const summary = manualEditEvidenceSummary({
      schema_version: "manual_edit_evidence.v1",
      event_count: 2,
      since: "2026-07-29T12:00:00Z",
      evidence_checksum: "sha256:manual-secret-checksum",
      by_event_type: {
        "timeline.asset.edited": 1,
        "asset.replaced": 1
      },
      events: [
        {
          event_id: "manual-event-secret-id",
          actor: "editor@example.test",
          details: {
            comment: "private edit note",
            replacement_asset_id: "asset-secret-id"
          }
        }
      ]
    });

    expect(summary).toBe(
      "2 edit events · types asset replacement:1,timeline edit:1 · post-failure · checksum"
    );
    expect(summary).not.toContain("manual-event-secret-id");
    expect(summary).not.toContain("editor@example.test");
    expect(summary).not.toContain("private edit note");
    expect(summary).not.toContain("asset-secret-id");
    expect(summary).not.toContain("manual-secret-checksum");
  });

  it("handles missing manual edit evidence quietly", () => {
    expect(manualEditEvidenceSummary(null)).toBe("no manual edit evidence");
    expect(manualEditEvidenceSummary({ event_count: 0 })).toBe("0 edit events");
  });

  it("explains production handoff blockers without exposing raw payloads", () => {
    expect(productionHandoffBlockerSummary("localized_output_not_approved")).toBe(
      "Approve every configured localized transcript before media handoff readiness."
    );
    expect(productionHandoffBlockerSummary("localized_output_qc_failing")).toBe(
      "Resolve failing localized transcript semantic-fidelity QC."
    );
    expect(productionHandoffBlockerSummary("discussion_structure_qc_failing")).toBe(
      "Resolve missing required topic coverage before downstream production."
    );
    expect(productionHandoffBlockerSummary("character_model_missing")).toBe(
      "Assign a model endpoint and model ID to every playable speaker."
    );
    expect(productionHandoffBlockerSummary("character_voice_asset_stale")).toBe(
      "Regenerate speech produced with an older speaker voice assignment."
    );
    expect(productionHandoffBlockerSummary("new_backend_blocker")).toBe(
      "Resolve this handoff blocker before continuing."
    );
  });

  it("summarizes the next operator workflow action", () => {
    expect(workflowNextActionSummary({ hasEpisode: false })).toMatchObject({
      status: "waiting",
      label: "Select an episode",
    });
    expect(
      workflowNextActionSummary({
        hasEpisode: true,
        pendingApprovalLabel: "Preview Render",
        canAdvanceWorkflow: true,
      })
    ).toMatchObject({
      status: "blocked",
      label: "Approve Preview Render",
    });
    expect(
      workflowNextActionSummary({
        hasEpisode: true,
        episodeStatus: "COMPLETED",
        completionReadinessStatus: "pass",
      })
    ).toMatchObject({
      status: "complete",
      label: "Workflow complete",
    });
    expect(
      workflowNextActionSummary({
        hasEpisode: true,
        canStartProduction: true,
        canAdvanceWorkflow: false,
      })
    ).toMatchObject({
      status: "ready",
      label: "Start production",
    });
    expect(
      workflowNextActionSummary({
        hasEpisode: true,
        completionReadinessStatus: "pass",
        canAdvanceWorkflow: true,
      })
    ).toMatchObject({
      status: "ready",
      label: "Complete workflow",
    });
    expect(
      workflowNextActionSummary({
        hasEpisode: true,
        episodeStatus: "FAILED",
        canAdvanceWorkflow: true,
      })
    ).toMatchObject({
      status: "blocked",
      label: "Retry failed stage",
    });
  });
});
