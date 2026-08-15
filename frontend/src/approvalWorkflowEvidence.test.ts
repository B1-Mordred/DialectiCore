import { describe, expect, it } from "vitest";
import {
  approvalActionLabel,
  approvalStageLabel,
  approvalWorkflowHandoffSummary,
  rejectionActionLabel,
} from "./approvalWorkflowEvidence";

describe("approval workflow evidence", () => {
  it("labels the production approval gates", () => {
    expect(approvalStageLabel("research_review")).toBe("Research review");
    expect(approvalStageLabel("transcript_review")).toBe("Transcript review");
    expect(approvalStageLabel("localized_transcript_review")).toBe(
      "Localized transcript review"
    );
    expect(approvalStageLabel("preview_render_review")).toBe("Preview render review");
    expect(approvalStageLabel("final_render_review")).toBe("Final render review");
    expect(approvalActionLabel("research_review")).toBe("Approve Research");
    expect(approvalActionLabel("transcript_review")).toBe("Approve Transcript");
    expect(approvalActionLabel("localized_transcript_review")).toBe(
      "Approve Localization"
    );
    expect(approvalActionLabel("preview_render_review")).toBe("Approve Preview");
    expect(approvalActionLabel("final_render_review")).toBe("Approve Final Render");
    expect(rejectionActionLabel("research_review")).toBe("Reject Research");
    expect(rejectionActionLabel("transcript_review")).toBe("Reject Transcript");
    expect(rejectionActionLabel("localized_transcript_review")).toBe(
      "Reject Localization"
    );
    expect(rejectionActionLabel("preview_render_review")).toBe("Reject Preview");
    expect(rejectionActionLabel("final_render_review")).toBe("Reject Final Render");
  });

  it("describes the workflow work unblocked by each approval", () => {
    expect(approvalWorkflowHandoffSummary("research_review")).toContain(
      "discussion preparation"
    );
    expect(approvalWorkflowHandoffSummary("transcript_review")).toContain("speech");
    expect(approvalWorkflowHandoffSummary("localized_transcript_review")).toContain(
      "localized speech"
    );
    expect(approvalWorkflowHandoffSummary("preview_render_review")).toContain("final render");
    expect(approvalWorkflowHandoffSummary("final_render_review")).toContain("publish handoff");
  });

  it("falls back safely for unknown approval stages", () => {
    expect(approvalStageLabel("custom_gate_review")).toBe("custom gate review");
    expect(approvalActionLabel("custom_gate_review")).toBe("Approve Gate");
    expect(rejectionActionLabel("custom_gate_review")).toBe("Reject Gate");
    expect(approvalWorkflowHandoffSummary("custom_gate_review")).toBe(
      "Approving records the gate decision for workflow evidence."
    );
  });
});
