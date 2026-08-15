import { describe, expect, it } from "vitest";
import {
  workflowRunUntilBlockedActionLabel,
  workflowReviewStageLabel,
  workflowRunUntilBlockedEvidenceSummary,
  workflowRunUntilBlockedStopReasonLabel,
} from "./workflowRunUntilBlockedEvidence";

describe("workflow run-until-blocked evidence", () => {
  it("summarizes missing evidence as waiting", () => {
    expect(workflowRunUntilBlockedEvidenceSummary(null)).toEqual({
      status: "waiting",
      label: "No bundled run",
      detail: "Use Run Until Review to produce until the next gate.",
      pendingApprovalStages: [],
      passCount: 0,
      progressedStageCount: 0,
      handoffStatus: null,
      nextHandoffAction: null,
      nextHandoffActionLabel: null,
      blockingReasons: [],
      readinessSummary: null,
      assetSummary: null,
    });
  });

  it("summarizes pending review without exposing raw approval ids", () => {
    const summary = workflowRunUntilBlockedEvidenceSummary({
      status: "awaiting_approval",
      stop_reason: "pending_approval",
      pass_count: 2,
      progressed_stage_count: 9,
      pending_approvals: [
        { stage: "preview_render_review" },
        { stage: "transcript_review" },
      ],
      summaries: [{ raw: "not rendered" }],
    });

    expect(summary).toEqual({
      status: "warning",
      label: "Awaiting review",
      detail: "2 passes · 9 progressed stages · awaiting Preview Render, Transcript",
      pendingApprovalStages: ["Preview Render", "Transcript"],
      passCount: 2,
      progressedStageCount: 9,
      handoffStatus: null,
      nextHandoffAction: null,
      nextHandoffActionLabel: null,
      blockingReasons: [],
      readinessSummary: null,
      assetSummary: null,
    });
    expect(summary.detail).not.toContain("approval");
  });

  it("summarizes persisted pending review evidence", () => {
    const summary = workflowRunUntilBlockedEvidenceSummary({
      status: "awaiting_approval",
      stop_reason: "pending_approval",
      pass_count: 1,
      progressed_stage_count: 6,
      pending_approval_stages: ["final_render_review"],
    });

    expect(summary).toMatchObject({
      status: "warning",
      label: "Awaiting review",
      detail: "1 passes · 6 progressed stages · awaiting Final Render",
      pendingApprovalStages: ["Final Render"],
    });
  });

  it("labels completed and blocked bundled runs", () => {
    expect(
      workflowRunUntilBlockedEvidenceSummary({
        status: "completed",
        stop_reason: "completed",
        pass_count: 1,
        progressed_stage_count: 3,
      })
    ).toMatchObject({
      status: "pass",
      label: "Production completed",
      detail: "1 passes · 3 progressed stages · completed",
    });

    expect(
      workflowRunUntilBlockedEvidenceSummary({
        status: "blocked",
        stop_reason: "stage_errors",
      })
    ).toMatchObject({
      status: "fail",
      label: "Production blocked",
      detail: "0 passes · 0 progressed stages · stage errors",
    });
  });

  it("summarizes compact talkshow handoff evidence", () => {
    const summary = workflowRunUntilBlockedEvidenceSummary({
      status: "blocked",
      stop_reason: "no_progress",
      pass_count: 3,
      progressed_stage_count: 7,
      handoff: {
        status: "blocked",
        next_handoff_action: "produce_remaining_speech_assets",
        blocking_reasons: [
          "completed_audio_missing",
          "completed_character_visual_missing",
        ],
        stage_readiness: {
          speech: false,
          character_animation: false,
          subtitles: true,
          timeline: null,
        },
        asset_ids: {
          preview_render: "preview-a",
          final_render: null,
          delivery_package: "package-a",
        },
      },
    });

    expect(summary).toMatchObject({
      status: "fail",
      handoffStatus: "blocked",
      nextHandoffAction: "produce_remaining_speech_assets",
      nextHandoffActionLabel: "produce remaining speech",
      blockingReasons: [
        "completed_audio_missing",
        "completed_character_visual_missing",
      ],
      readinessSummary: "speech blocked · characters blocked · subtitles ready",
      assetSummary: "preview · package",
    });
  });

  it("normalizes stop reasons and review stage labels", () => {
    expect(workflowRunUntilBlockedStopReasonLabel("max_passes_reached")).toBe(
      "pass limit reached"
    );
    expect(workflowRunUntilBlockedStopReasonLabel("new_reason")).toBe("unknown stop");
    expect(workflowReviewStageLabel("localized_transcript_review")).toBe("Localization");
    expect(workflowReviewStageLabel("custom_review_gate")).toBe("custom review gate");
    expect(workflowRunUntilBlockedActionLabel("review_final_render")).toBe(
      "review final render"
    );
    expect(workflowRunUntilBlockedActionLabel("new_action_value")).toBe(
      "new action value"
    );
  });
});
