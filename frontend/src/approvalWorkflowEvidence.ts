export function approvalStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    research_review: "Research review",
    transcript_review: "Transcript review",
    localized_transcript_review: "Localized transcript review",
    preview_render_review: "Preview render review",
    final_render_review: "Final render review",
  };
  return labels[stage] ?? stage.replace(/_/g, " ");
}

export function approvalWorkflowHandoffSummary(stage: string): string {
  const summaries: Record<string, string> = {
    research_review: "Approving returns the episode to draft for discussion preparation.",
    transcript_review:
      "Approving marks the transcript ready for localization, QC, speech, and visuals.",
    localized_transcript_review:
      "Approving unlocks localized speech, subtitles, visuals, timeline, and render work.",
    preview_render_review: "Approving allows the final render worker pass.",
    final_render_review:
      "Approving allows thumbnail, package, manifest, and publish handoff work.",
  };
  return summaries[stage] ?? "Approving records the gate decision for workflow evidence.";
}

export function approvalActionLabel(stage: string): string {
  const labels: Record<string, string> = {
    research_review: "Approve Research",
    transcript_review: "Approve Transcript",
    localized_transcript_review: "Approve Localization",
    preview_render_review: "Approve Preview",
    final_render_review: "Approve Final Render",
  };
  return labels[stage] ?? "Approve Gate";
}

export function rejectionActionLabel(stage: string): string {
  const labels: Record<string, string> = {
    research_review: "Reject Research",
    transcript_review: "Reject Transcript",
    localized_transcript_review: "Reject Localization",
    preview_render_review: "Reject Preview",
    final_render_review: "Reject Final Render",
  };
  return labels[stage] ?? "Reject Gate";
}
