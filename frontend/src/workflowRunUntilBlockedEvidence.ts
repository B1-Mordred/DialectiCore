export type WorkflowRunUntilBlockedEvidenceLike = {
  recorded_at?: string | null;
  status?: string | null;
  stop_reason?: string | null;
  pass_count?: number | null;
  progressed_stage_count?: number | null;
  handoff?: WorkflowRunUntilBlockedHandoffLike | null;
  pending_approvals?: Array<{ stage?: string | null } | null> | null;
  pending_approval_stages?: Array<string | null> | null;
  completion_readiness?: { status?: string | null; failed_checks?: string[] | null } | null;
  summaries?: unknown[] | null;
};

export type WorkflowRunUntilBlockedHandoffLike = {
  status?: string | null;
  blocking_reasons?: Array<string | null> | null;
  next_handoff_action?: string | null;
  stage_readiness?: Record<string, boolean | null | undefined> | null;
  asset_ids?: Record<string, string | null | undefined> | null;
};

export type WorkflowRunUntilBlockedEvidenceSummary = {
  status: "pass" | "warning" | "fail" | "waiting";
  label: string;
  detail: string;
  pendingApprovalStages: string[];
  passCount: number;
  progressedStageCount: number;
  handoffStatus: string | null;
  nextHandoffAction: string | null;
  nextHandoffActionLabel: string | null;
  blockingReasons: string[];
  readinessSummary: string | null;
  assetSummary: string | null;
};

export function workflowRunUntilBlockedEvidenceSummary(
  evidence: WorkflowRunUntilBlockedEvidenceLike | null,
): WorkflowRunUntilBlockedEvidenceSummary {
  if (!evidence) {
    return {
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
    };
  }

  const rawPendingStages =
    evidence.pending_approval_stages ??
    (evidence.pending_approvals ?? []).map((approval) => approval?.stage);
  const pendingApprovalStages = rawPendingStages
    .filter((stage): stage is string => Boolean(stage && stage.trim()))
    .map((stage) => workflowReviewStageLabel(stage));
  const passCount = safeCount(evidence.pass_count);
  const progressedStageCount = safeCount(evidence.progressed_stage_count);
  const stopReason = evidence.stop_reason ?? "unknown";
  const status = evidence.status ?? "unknown";
  const handoff = evidence.handoff && typeof evidence.handoff === "object"
    ? evidence.handoff
    : null;
  const nextHandoffAction =
    typeof handoff?.next_handoff_action === "string" && handoff.next_handoff_action.trim()
      ? handoff.next_handoff_action
      : null;

  return {
    status: workflowRunUntilBlockedStatusClass(status, stopReason),
    label: workflowRunUntilBlockedLabel(status, stopReason),
    detail: [
      `${passCount} passes`,
      `${progressedStageCount} progressed stages`,
      pendingApprovalStages.length > 0
        ? `awaiting ${pendingApprovalStages.join(", ")}`
        : workflowRunUntilBlockedStopReasonLabel(stopReason),
    ].join(" · "),
    pendingApprovalStages,
    passCount,
    progressedStageCount,
    handoffStatus:
      typeof handoff?.status === "string" && handoff.status.trim() ? handoff.status : null,
    nextHandoffAction,
    nextHandoffActionLabel: nextHandoffAction
      ? workflowRunUntilBlockedActionLabel(nextHandoffAction)
      : null,
    blockingReasons: Array.isArray(handoff?.blocking_reasons)
      ? handoff.blocking_reasons
          .filter((reason): reason is string => Boolean(reason && reason.trim()))
          .slice(0, 6)
      : [],
    readinessSummary: workflowRunUntilBlockedReadinessSummary(handoff?.stage_readiness),
    assetSummary: workflowRunUntilBlockedAssetSummary(handoff?.asset_ids),
  };
}

export function workflowRunUntilBlockedStopReasonLabel(reason?: string | null): string {
  const labels: Record<string, string> = {
    completed: "completed",
    pending_approval: "pending review",
    stage_errors: "stage errors",
    no_progress: "no progress",
    workflow_run_missing: "missing workflow run",
    max_passes_reached: "pass limit reached",
    cancelled: "cancelled",
  };
  return labels[reason ?? ""] ?? "unknown stop";
}

export function workflowReviewStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    research_review: "Research",
    transcript_review: "Transcript",
    localized_transcript_review: "Localization",
    preview_render_review: "Preview Render",
    final_render_review: "Final Render",
  };
  return labels[stage] ?? stage.replace(/_/g, " ");
}

export function workflowRunUntilBlockedActionLabel(action: string): string {
  const labels: Record<string, string> = {
    approve_or_generate_broadcast_transcript: "approve or generate transcript",
    approve_broadcast_transcript: "approve transcript",
    regenerate_discussion_turns: "regenerate discussion",
    map_unknown_speakers_to_participants: "map speakers",
    configure_character_model_sources: "configure character models",
    rerun_discussion_after_model_changes: "rerun discussion",
    configure_character_voice_profiles: "configure character voices",
    regenerate_stale_speech_assets: "regenerate stale speech",
    produce_remaining_speech_assets: "produce remaining speech",
    configure_character_visual_profiles: "configure character visuals",
    regenerate_stale_character_visuals: "regenerate stale visuals",
    produce_remaining_character_visuals: "produce character visuals",
    produce_character_reaction_loop_assets: "produce reaction loops",
    produce_studio_scene_assets: "produce studio scenes",
    generate_subtitles: "generate subtitles",
    build_episode_timeline: "build timeline",
    repair_episode_timeline_segments: "repair timeline",
    generate_localized_outputs: "generate localizations",
    approve_localized_outputs: "approve localizations",
    run_localized_output_qc: "run localization QC",
    repair_localized_outputs: "repair localizations",
    render_preview_or_final_video: "render video",
    repair_preview_render: "repair preview render",
    repair_final_render: "repair final render",
    inspect_or_regenerate_youtube_export_package: "inspect package",
    repair_youtube_export_package: "repair package",
    generate_thumbnail: "generate thumbnail",
    regenerate_youtube_export_package: "regenerate package",
    regenerate_production_manifest: "regenerate manifest",
    run_publish_delivery_qc: "run publish QC",
    repair_publish_delivery: "repair publish delivery",
    repair_or_approve_claim_qc: "repair or approve claim QC",
    review_preview_render: "review preview render",
    review_final_render: "review final render",
    inspect_render_for_next_review_gate: "inspect render",
    complete_workflow_or_inspect_publish_evidence: "complete or inspect publish",
    continue_workflow_to_render: "continue to render",
    wait_for_or_repair_publish_job: "wait or repair publish",
    inspect_workflow_handoff: "inspect workflow handoff",
  };
  return labels[action] ?? action.replace(/_/g, " ");
}

function workflowRunUntilBlockedReadinessSummary(
  readiness?: Record<string, boolean | null | undefined> | null,
): string | null {
  if (!readiness || typeof readiness !== "object") {
    return null;
  }
  const labels: Array<[string, string]> = [
    ["speech", "speech"],
    ["character_animation", "characters"],
    ["studio_scene", "studio"],
    ["subtitles", "subtitles"],
    ["timeline", "timeline"],
    ["publish", "publish"],
  ];
  const values = labels
    .map(([key, label]) => {
      const ready = readiness[key];
      if (typeof ready !== "boolean") {
        return null;
      }
      return `${label} ${ready ? "ready" : "blocked"}`;
    })
    .filter((value): value is string => Boolean(value));
  return values.length ? values.slice(0, 4).join(" · ") : null;
}

function workflowRunUntilBlockedAssetSummary(
  assetIds?: Record<string, string | null | undefined> | null,
): string | null {
  if (!assetIds || typeof assetIds !== "object") {
    return null;
  }
  const present = [
    ["preview_render", "preview"],
    ["final_render", "final"],
    ["delivery_package", "package"],
    ["production_manifest", "manifest"],
    ["publish_job", "publish"],
  ]
    .filter(([key]) => Boolean(assetIds[key]))
    .map(([, label]) => label);
  return present.length ? present.join(" · ") : null;
}

function workflowRunUntilBlockedStatusClass(status: string, stopReason: string) {
  if (status === "completed" || stopReason === "completed") {
    return "pass";
  }
  if (status === "blocked" || status === "cancelled" || stopReason === "stage_errors") {
    return "fail";
  }
  if (status === "awaiting_approval" || status === "ready_to_complete") {
    return "warning";
  }
  return "waiting";
}

function workflowRunUntilBlockedLabel(status: string, stopReason: string): string {
  if (status === "completed" || stopReason === "completed") {
    return "Production completed";
  }
  if (status === "awaiting_approval" || stopReason === "pending_approval") {
    return "Awaiting review";
  }
  if (status === "ready_to_complete") {
    return "Ready to complete";
  }
  if (status === "blocked" || stopReason === "stage_errors") {
    return "Production blocked";
  }
  if (status === "cancelled" || stopReason === "cancelled") {
    return "Production cancelled";
  }
  return "Run recorded";
}

function safeCount(value: number | null | undefined): number {
  return typeof value === "number" && Number.isFinite(value) ? value : 0;
}
