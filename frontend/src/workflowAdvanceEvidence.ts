export type WorkflowRunStartSummary = {
  schema_version?: string;
  episodes_scanned?: number;
  workflow_runs_started?: number;
  skipped?: number;
  error_count?: number;
  errors?: unknown[];
};

export type WorkflowAdmissionSummary = {
  schema_version?: string;
  episodes_scanned?: number;
  active_run_episode_count?: number;
  missing_run_episode_count?: number;
  blocked_episode_count?: number;
  stage_execution_requires_running_workflow_run?: boolean;
};

export type ManualEditEvidenceLike = {
  schema_version?: string;
  event_count?: number;
  by_event_type?: Record<string, number>;
  since?: string | null;
  evidence_checksum?: string | null;
};

type WorkflowAdvanceLike = {
  episode?: { id?: string };
  summary?: {
    workflow_run_starts?: unknown;
    workflow_admission?: unknown;
  };
};

export type WorkflowNextActionInput = {
  hasEpisode: boolean;
  episodeStatus?: string | null;
  workflowPaused?: boolean;
  pendingApprovalLabel?: string | null;
  completionReadinessStatus?: string | null;
  canStartProduction?: boolean;
  canAdvanceWorkflow?: boolean;
};

export type WorkflowNextActionSummary = {
  status: "ready" | "blocked" | "complete" | "waiting";
  label: string;
  detail: string;
};

export function workflowNextActionSummary(
  input: WorkflowNextActionInput
): WorkflowNextActionSummary {
  const episodeStatus = input.episodeStatus ?? null;
  if (!input.hasEpisode) {
    return {
      status: "waiting",
      label: "Select an episode",
      detail: "Choose or create a talk-show episode before running production.",
    };
  }
  if (episodeStatus === "COMPLETED") {
    return {
      status: "complete",
      label: "Workflow complete",
      detail: "The talk-show package has passed completion gates and is closed.",
    };
  }
  if (episodeStatus === "CANCELLED") {
    return {
      status: "blocked",
      label: "Workflow cancelled",
      detail: "Create a new episode or resume from a non-cancelled version.",
    };
  }
  if (episodeStatus === "FAILED") {
    return {
      status: "blocked",
      label: "Retry failed stage",
      detail: "Use Retry Workflow after reviewing the failed stage evidence.",
    };
  }
  if (input.workflowPaused) {
    return {
      status: "blocked",
      label: "Resume workflow",
      detail: "The selected run is paused and will not advance until resumed.",
    };
  }
  if (input.pendingApprovalLabel) {
    return {
      status: "blocked",
      label: `Approve ${input.pendingApprovalLabel}`,
      detail: "A human approval gate is holding the next production stage.",
    };
  }
  if (input.completionReadinessStatus === "pass") {
    return {
      status: "ready",
      label: "Complete workflow",
      detail: "All completion gates pass; close the production run.",
    };
  }
  if (input.canStartProduction) {
    return {
      status: "ready",
      label: "Start production",
      detail: "Begin the durable talk-show workflow for this episode.",
    };
  }
  if (input.canAdvanceWorkflow) {
    return {
      status: "ready",
      label: "Advance workflow",
      detail: "Run the next ordered stage: discussion, speech, visuals, render, or delivery.",
    };
  }
  return {
    status: "waiting",
    label: "Awaiting prerequisite",
    detail: "Review workflow evidence and readiness checks for the next blocker.",
  };
}

export function workflowRunStartSummaryFromAdvance(
  latestAdvance: WorkflowAdvanceLike | null,
  episodeId?: string | null
): WorkflowRunStartSummary | null {
  if (!latestAdvance || !episodeId || latestAdvance.episode?.id !== episodeId) {
    return null;
  }
  const summary = latestAdvance.summary?.workflow_run_starts;
  if (!summary || typeof summary !== "object" || Array.isArray(summary)) {
    return null;
  }
  return summary as WorkflowRunStartSummary;
}

export function workflowRunStartStatus(summary: WorkflowRunStartSummary | null): string {
  if (!summary) {
    return "no advance evidence";
  }
  if ((summary.error_count ?? 0) > 0) {
    return "run start error";
  }
  if ((summary.workflow_runs_started ?? 0) > 0) {
    return "durable run started";
  }
  if ((summary.episodes_scanned ?? 0) > 0 && (summary.skipped ?? 0) > 0) {
    return "existing run or gated state";
  }
  return "no eligible episode scanned";
}

export function workflowRunStartSummaryText(summary: WorkflowRunStartSummary | null): string {
  if (!summary) {
    return "advance the workflow to inspect run-start evidence";
  }
  return [
    `${summary.workflow_runs_started ?? 0} started`,
    `${summary.episodes_scanned ?? 0} scanned`,
    `${summary.skipped ?? 0} skipped`,
    `${summary.error_count ?? 0} errors`,
  ].join(" · ");
}

export function workflowAdmissionSummaryFromAdvance(
  latestAdvance: WorkflowAdvanceLike | null,
  episodeId?: string | null
): WorkflowAdmissionSummary | null {
  if (!latestAdvance || !episodeId || latestAdvance.episode?.id !== episodeId) {
    return null;
  }
  const summary = latestAdvance.summary?.workflow_admission;
  if (!summary || typeof summary !== "object" || Array.isArray(summary)) {
    return null;
  }
  return summary as WorkflowAdmissionSummary;
}

export function workflowAdmissionStatus(summary: WorkflowAdmissionSummary | null): string {
  if (!summary) {
    return "no admission evidence";
  }
  if ((summary.active_run_episode_count ?? 0) > 0) {
    return "stage execution admitted";
  }
  if ((summary.missing_run_episode_count ?? 0) > 0) {
    return "start durable run first";
  }
  if ((summary.blocked_episode_count ?? 0) > 0) {
    return "workflow blocked";
  }
  return "no eligible episode scanned";
}

export function workflowAdmissionSummaryText(
  summary: WorkflowAdmissionSummary | null
): string {
  if (!summary) {
    return "advance the workflow to inspect stage admission evidence";
  }
  return [
    `${summary.active_run_episode_count ?? 0} active`,
    `${summary.missing_run_episode_count ?? 0} missing run`,
    `${summary.blocked_episode_count ?? 0} blocked`,
    `${summary.episodes_scanned ?? 0} scanned`,
  ].join(" · ");
}

export function manualEditEvidenceSummary(evidence: unknown): string {
  if (!evidence || typeof evidence !== "object" || Array.isArray(evidence)) {
    return "no manual edit evidence";
  }
  const data = evidence as ManualEditEvidenceLike;
  const eventCount =
    typeof data.event_count === "number" && Number.isFinite(data.event_count)
      ? data.event_count
      : 0;
  const byType =
    data.by_event_type && typeof data.by_event_type === "object"
      ? Object.entries(data.by_event_type)
          .filter(([, value]) => typeof value === "number" && Number.isFinite(value))
          .map(([key, value]) => `${manualEditEventTypeLabel(key)}:${value}`)
          .sort()
          .join(",")
      : "";
  const since = typeof data.since === "string" && data.since.trim() ? "post-failure" : "";
  const checksum =
    typeof data.evidence_checksum === "string" && data.evidence_checksum.trim()
      ? "checksum"
      : "";
  return [
    `${eventCount} edit events`,
    byType ? `types ${byType}` : "",
    since,
    checksum,
  ]
    .filter(Boolean)
    .join(" · ");
}

function manualEditEventTypeLabel(eventType: string): string {
  const labels: Record<string, string> = {
    "asset.replaced": "asset replacement",
    "timeline.asset.edited": "timeline edit",
    "transcript.turn.regenerated": "turn regeneration",
    "transcript.turn.excluded": "turn exclusion",
  };
  return labels[eventType] ?? "manual edit";
}

export function productionHandoffBlockerSummary(reason: string): string {
  const summaries: Record<string, string> = {
    approved_transcript_missing: "Run discussion and approve the transcript.",
    transcript_not_approved: "Approve the selected transcript before downstream production.",
    discussion_structure_qc_missing:
      "Run discussion structure QC before downstream production.",
    discussion_structure_qc_failing:
      "Resolve missing required topic coverage before downstream production.",
    playable_turns_missing: "The transcript has no playable turns.",
    character_profile_missing: "Restore or assign the missing speaker participant profile.",
    character_model_missing: "Assign a model endpoint and model ID to every playable speaker.",
    character_model_turn_stale:
      "Regenerate turns produced with an older speaker model assignment.",
    character_voice_missing: "Assign a voice profile to every playable speaker.",
    character_voice_asset_stale:
      "Regenerate speech produced with an older speaker voice assignment.",
    character_visual_missing: "Assign a visual profile to every playable speaker.",
    character_visual_asset_stale:
      "Regenerate character visuals produced with an older visual assignment.",
    completed_audio_missing: "Generate or sync completed speech assets for every playable turn.",
    completed_character_visual_missing:
      "Generate completed primary character visuals for every playable turn.",
    shot_planned_reaction_loop_missing:
      "Generate and link completed reusable reaction loops for shot-planned character segments.",
    shot_planned_studio_scene_missing:
      "Generate and link the completed reusable studio scene for shot-planned segments.",
    localized_output_missing: "Create every configured localized transcript.",
    localized_output_not_approved:
      "Approve every configured localized transcript before media handoff readiness.",
    localized_output_qc_missing:
      "Run semantic-fidelity QC for every configured localized transcript.",
    localized_output_qc_failing:
      "Resolve failing localized transcript semantic-fidelity QC.",
    subtitle_asset_missing: "Generate subtitles after speech assets complete.",
    timeline_asset_missing: "Build the timeline after audio, subtitles, and visuals are ready.",
    timeline_segments_missing: "Timeline segments do not cover all playable turns.",
    render_asset_missing: "Render a preview or final video from the completed timeline.",
    preview_render_qc_failed: "Resolve preview render QC failures before approval.",
    final_render_qc_failed: "Resolve final render QC failures before delivery.",
    export_package_qc_missing: "Run package QC before delivery.",
    export_package_qc_failed: "Resolve package QC failures before delivery.",
    thumbnail_missing: "Generate a thumbnail from the approved final render.",
    export_package_thumbnail_missing:
      "Regenerate the YouTube package so it includes the checked thumbnail.",
    export_package_subtitles_missing:
      "Regenerate the YouTube package so it includes subtitles/captions.",
    production_manifest_invalid: "Regenerate the production manifest before delivery.",
    production_manifest_publish_evidence_missing:
      "Refresh the production manifest so it includes publish job and QC evidence.",
    publish_delivery_qc_missing: "Run publish delivery QC before delivery.",
    publish_delivery_qc_failed: "Resolve blocking publish delivery QC before delivery.",
    claim_qc_failed: "Resolve blocking claim/citation QC before delivery.",
  };
  return summaries[reason] ?? "Resolve this handoff blocker before continuing.";
}
