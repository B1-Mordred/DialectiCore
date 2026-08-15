export type ProductionReportActionEntry = {
  scope?: string;
  action?: string;
  status?: string | null;
  failed_check_count?: number;
  asset_count?: number;
  stop_reason?: string | null;
  pending_approval_stages?: string[];
  blocking_reasons?: string[];
  blocking_sections?: string[];
  model_failed_count?: number;
  voicebox_failed_count?: number;
};

export type ProductionReportForActionRows = {
  operator_next_action?: string | null;
  operator_next_actions?: ProductionReportActionEntry[];
  provider_repair_handoff?: {
    status?: string | null;
    path?: string | null;
    exists?: boolean;
    section_count?: number;
    latest_sections?: string[];
    has_voicebox_requirements?: boolean;
    has_managed_media_requirements?: boolean;
  };
};

export type ProductionReportActionRow = {
  key: string;
  scope: string;
  title: string;
  status: string;
  detail: string;
  priority: "pass" | "warning" | "fail";
};

export function productionReportActionRows(
  report: ProductionReportForActionRows | null | undefined
): ProductionReportActionRow[] {
  if (!report) {
    return [];
  }
  const entries = normalizedEntries(report);
  return entries.map((entry, index) => {
    const status = entry.status || "pending";
    return {
      key: `${entry.scope ?? "general"}:${entry.action ?? "inspect"}:${index}`,
      scope: scopeLabel(entry.scope),
      title: actionLabel(entry.action),
      status,
      detail: actionDetail(entry, report),
      priority: actionPriority(status)
    };
  });
}

function normalizedEntries(report: ProductionReportForActionRows): ProductionReportActionEntry[] {
  if (report.operator_next_actions?.length) {
    return report.operator_next_actions.filter((entry) => Boolean(entry.action || entry.scope));
  }
  if (report.operator_next_action) {
    return [
      {
        scope: "next",
        action: report.operator_next_action,
        status: "pending"
      }
    ];
  }
  return [];
}

function actionDetail(
  entry: ProductionReportActionEntry,
  report: ProductionReportForActionRows
): string {
  const parts = [
    scopeDescription(entry.scope),
    liveProviderPreflightDetail(entry),
    workflowHandoffDetail(entry),
    providerRepairHandoffDetail(entry, report),
    typeof entry.asset_count === "number" ? `${entry.asset_count} assets` : "",
    typeof entry.failed_check_count === "number" ? `${entry.failed_check_count} checks` : ""
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" · ") : "Inspect the production report evidence.";
}

function providerRepairHandoffDetail(
  entry: ProductionReportActionEntry,
  report: ProductionReportForActionRows
): string {
  if (!actionUsesB1ProviderRepair(entry.action)) {
    return "";
  }
  const handoff = report.provider_repair_handoff;
  if (!handoff) {
    return "Repair handoff not checked";
  }
  if (handoff.status === "not_configured") {
    return "Repair handoff not configured";
  }
  if (handoff.status === "missing" || handoff.exists === false) {
    return `Repair handoff missing at ${handoff.path ?? "configured path"}`;
  }
  if (handoff.status === "unreadable") {
    return `Repair handoff unreadable at ${handoff.path ?? "configured path"}`;
  }
  const latestSections = handoff.latest_sections ?? [];
  const latestSection = latestSections.length > 0 ? latestSections[latestSections.length - 1] : "";
  const providerFlags = [
    handoff.has_voicebox_requirements ? "voicebox noted" : "",
    handoff.has_managed_media_requirements ? "B1 media noted" : ""
  ].filter(Boolean);
  return [
    `Repair handoff present at ${handoff.path ?? "configured path"}`,
    typeof handoff.section_count === "number" ? `${handoff.section_count} sections` : "",
    ...providerFlags,
    latestSection ? `latest ${latestSection}` : ""
  ]
    .filter(Boolean)
    .join(", ");
}

function actionUsesB1ProviderRepair(action?: string): boolean {
  return (
    action === "fix_voicebox_generation_then_retry_audio_assets" ||
    action === "fix_b1_managed_media_runner_then_retry_visual_assets" ||
    action === "fix_b1_managed_media_runner_then_rerun_smoke" ||
    action === "retry_managed_media_visual_assets_after_provider_fix" ||
    action === "retry_fallback_visuals_as_native_after_b1_fix"
  );
}

function actionPriority(status: string): ProductionReportActionRow["priority"] {
  if (["pass", "ready", "completed", "fallback_visuals_present"].includes(status)) {
    return "pass";
  }
  if (
    [
      "fail",
      "failed",
      "runner_failed",
      "not_downloadable",
      "not_inspectable",
      "missing",
      "blocked"
    ].includes(status)
  ) {
    return "fail";
  }
  return "warning";
}

function scopeLabel(scope?: string): string {
  if (scope === "managed_media") {
    return "B1 Media";
  }
  if (scope === "native_visual") {
    return "Native Visuals";
  }
  if (scope === "delivery_artifacts") {
    return "Delivery Artifacts";
  }
  if (scope === "export_package") {
    return "Export Package";
  }
  if (scope === "publishing") {
    return "Publishing";
  }
  if (scope === "completion") {
    return "Completion";
  }
  if (scope === "acceptance") {
    return "Acceptance";
  }
  if (scope === "speech") {
    return "Speech";
  }
  if (scope === "workflow") {
    return "Workflow";
  }
  if (scope === "live_provider_preflight") {
    return "Live Providers";
  }
  return "Next Action";
}

function scopeDescription(scope?: string): string {
  if (scope === "speech") {
    return "Speech generation and Voicebox output";
  }
  if (scope === "managed_media") {
    return "B1 managed media execution";
  }
  if (scope === "native_visual") {
    return "Character animation and native visual handoff";
  }
  if (scope === "delivery_artifacts") {
    return "Render/package/manifest downloadability";
  }
  if (scope === "export_package") {
    return "YouTube ZIP inspection";
  }
  if (scope === "publishing") {
    return "Dry-run or live publish handoff";
  }
  if (scope === "completion") {
    return "Production completion gates";
  }
  if (scope === "acceptance") {
    return "Real-life test acceptance";
  }
  if (scope === "workflow") {
    return "Run Until Review handoff";
  }
  if (scope === "live_provider_preflight") {
    return "Frontier cast model and voice preflight";
  }
  return "";
}

function liveProviderPreflightDetail(entry: ProductionReportActionEntry): string {
  if (entry.scope !== "live_provider_preflight") {
    return "";
  }
  const sections = (entry.blocking_sections ?? [])
    .filter(Boolean)
    .slice(0, 3)
    .map((section) => section.replace(/_/g, " "));
  return [
    sections.length ? `blocked ${sections.join(", ")}` : "",
    typeof entry.model_failed_count === "number"
      ? `${entry.model_failed_count} model failed`
      : "",
    typeof entry.voicebox_failed_count === "number"
      ? `${entry.voicebox_failed_count} voice failed`
      : "",
  ]
    .filter(Boolean)
    .join(", ");
}

function workflowHandoffDetail(entry: ProductionReportActionEntry): string {
  if (entry.scope !== "workflow") {
    return "";
  }
  const pendingStages = (entry.pending_approval_stages ?? [])
    .filter(Boolean)
    .slice(0, 3)
    .map(workflowStageLabel);
  const blockers = (entry.blocking_reasons ?? [])
    .filter(Boolean)
    .slice(0, 3)
    .map((reason) => reason.replace(/_/g, " "));
  return [
    entry.stop_reason ? `stopped at ${entry.stop_reason.replace(/_/g, " ")}` : "",
    pendingStages.length ? `review ${pendingStages.join(", ")}` : "",
    blockers.length ? `blockers ${blockers.join(", ")}` : "",
  ]
    .filter(Boolean)
    .join(", ");
}

function workflowStageLabel(stage: string): string {
  const labels: Record<string, string> = {
    research_review: "Research",
    transcript_review: "Transcript",
    localized_transcript_review: "Localization",
    preview_render_review: "Preview Render",
    final_render_review: "Final Render",
  };
  return labels[stage] ?? stage.replace(/_/g, " ");
}

function actionLabel(action?: string): string {
  if (action === "inspect_export_package_and_publish_evidence") {
    return "Inspect Package And Publish Evidence";
  }
  if (action === "fix_voicebox_generation_then_retry_audio_assets") {
    return "Fix Voicebox, Then Retry Speech";
  }
  if (action === "plan_or_produce_speech_assets") {
    return "Plan Or Produce Speech";
  }
  if (action === "produce_speech_assets") {
    return "Produce Speech";
  }
  if (action === "sync_voicebox_jobs") {
    return "Sync Voicebox Jobs";
  }
  if (action === "reset_cancelled_audio_assets_for_retry") {
    return "Reset Cancelled Speech";
  }
  if (action === "produce_remaining_speech_assets") {
    return "Produce Remaining Speech";
  }
  if (action === "run_b1_managed_media_smoke_or_start_native_visual_production") {
    return "Run B1 Smoke Or Start Native Visuals";
  }
  if (action === "fix_b1_managed_media_runner_then_retry_visual_assets") {
    return "Fix B1 Runner, Then Retry Visuals";
  }
  if (action === "fix_b1_managed_media_runner_then_rerun_smoke") {
    return "Fix B1 Runner, Then Rerun Smoke";
  }
  if (action === "sync_managed_media_jobs") {
    return "Sync B1 Media Jobs";
  }
  if (action === "retry_managed_media_visual_assets_after_provider_fix") {
    return "Retry B1 Visual Assets";
  }
  if (action === "retry_fallback_visuals_as_native_after_b1_fix") {
    return "Retry Fallback Visuals As Native";
  }
  if (action === "produce_native_visual_assets") {
    return "Produce Native Visuals";
  }
  if (action === "restore_or_regenerate_missing_delivery_artifacts") {
    return "Restore Or Regenerate Delivery Artifacts";
  }
  if (action === "inspect_or_regenerate_youtube_export_package") {
    return "Inspect Or Regenerate YouTube Package";
  }
  if (action === "run_dry_run_publish_for_real_life_test") {
    return "Run Dry-Run Publish";
  }
  if (action === "wait_for_or_repair_publish_job") {
    return "Wait For Or Repair Publish Job";
  }
  if (action === "resolve_completion_readiness_blockers") {
    return "Resolve Completion Blockers";
  }
  if (action === "resolve_blockers_before_real_life_test") {
    return "Resolve Real-Life Test Blockers";
  }
  if (action === "approve_or_generate_broadcast_transcript") {
    return "Approve Or Generate Transcript";
  }
  if (action === "approve_broadcast_transcript") {
    return "Approve Transcript";
  }
  if (action === "review_preview_render") {
    return "Review Preview Render";
  }
  if (action === "review_final_render") {
    return "Review Final Render";
  }
  if (action === "configure_character_model_sources") {
    return "Configure Character Models";
  }
  if (action === "configure_character_voice_profiles") {
    return "Configure Character Voices";
  }
  if (action === "configure_character_visual_profiles") {
    return "Configure Character Visuals";
  }
  if (action === "produce_remaining_character_visuals") {
    return "Produce Character Visuals";
  }
  if (action === "produce_character_reaction_loop_assets") {
    return "Produce Reaction Loops";
  }
  if (action === "produce_studio_scene_assets") {
    return "Produce Studio Scenes";
  }
  if (action === "build_episode_timeline") {
    return "Build Timeline";
  }
  if (action === "render_preview_or_final_video") {
    return "Render Video";
  }
  if (action === "run_live_provider_preflight_before_real_life_test") {
    return "Run Live Provider Preflight";
  }
  if (action === "fix_voicebox_generation_then_rerun_live_preflight") {
    return "Fix Voicebox, Then Rerun Preflight";
  }
  if (action === "fix_model_provider_then_rerun_live_preflight") {
    return "Fix Model Provider, Then Rerun Preflight";
  }
  if (action === "fix_live_provider_failures_then_rerun_preflight") {
    return "Fix Live Providers, Then Rerun Preflight";
  }
  if (action === "inspect_live_provider_preflight") {
    return "Inspect Live Provider Preflight";
  }
  return titleize(action ?? "Inspect Workflow");
}

function titleize(value: string): string {
  return value
    .split(/[_\s-]+/)
    .filter(Boolean)
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}
