export function authSettingsDetailRows(settings: Record<string, unknown>): Array<[string, string]> {
  if (!hasAuthSettings(settings)) {
    return [];
  }
  return [
    [
      "Auth Mode",
      [
        settings.auth_enabled === true ? "enabled" : "disabled",
        settings.auth_api_key_reference_configured === true ? "api-key reference" : "api-key missing"
      ].join(" - ")
    ],
    [
      "API Key Headers",
      [
        `role ${formatSettingsLabel(settings.auth_role_header)}`,
        `user ${formatSettingsLabel(settings.auth_user_header)}`
      ].join(" - ")
    ],
    [
      "Trusted Identity",
      [
        settings.auth_trusted_identity_enabled === true ? "enabled" : "off",
        `identity ${formatSettingsLabel(settings.auth_trusted_identity_header)}`,
        `email ${formatSettingsLabel(settings.auth_trusted_email_header)}`,
        `groups ${formatSettingsLabel(settings.auth_trusted_groups_header)}`,
        `default ${formatSettingsLabel(settings.auth_trusted_default_role)}`,
        settings.auth_trusted_group_role_map_configured === true ? "group roles" : "no group roles"
      ].join(" - ")
    ],
    [
      "Provider Sessions",
      [
        settings.auth_provider_session_enabled === true ? "enabled" : "off",
        settings.auth_provider_session_introspection_configured === true
          ? "introspection"
          : "no introspection",
        settings.auth_provider_session_client_id_reference_configured === true
          ? "client id ref"
          : "no client id ref",
        settings.auth_provider_session_client_secret_reference_configured === true
          ? "client secret ref"
          : "no client secret ref"
      ].join(" - ")
    ],
    [
      "Provider Claims",
      [
        `token ${formatSettingsLabel(settings.auth_provider_session_token_header)}`,
        `user ${formatSettingsLabel(settings.auth_provider_session_user_claim)}`,
        `groups ${formatSettingsLabel(settings.auth_provider_session_groups_claim)}`,
        `default ${formatSettingsLabel(settings.auth_provider_session_default_role)}`,
        settings.auth_provider_session_group_role_map_configured === true
          ? "group roles"
          : "no group roles"
      ].join(" - ")
    ]
  ];
}

export function objectStorageSettingsDetailRows(
  settings: Record<string, unknown>
): Array<[string, string]> {
  if (!hasObjectStorageSettings(settings)) {
    return [];
  }
  return [
    [
      "Object Storage Settings",
      [
        formatSettingsLabel(settings.object_storage_backend),
        settings.object_storage_bucket_configured === true ? "bucket configured" : "bucket missing",
        settings.object_storage_endpoint_configured === true
          ? "endpoint configured"
          : "endpoint missing"
      ].join(" - ")
    ],
    [
      "Object Storage Credentials",
      [
        settings.object_storage_credential_pair_configured === true
          ? "paired"
          : "incomplete",
        settings.object_storage_access_key_reference_configured === true
          ? "access ref"
          : "no access ref",
        settings.object_storage_secret_key_reference_configured === true
          ? "secret ref"
          : "no secret ref"
      ].join(" - ")
    ],
    [
      "Object Storage Options",
      [
        settings.object_storage_region_configured === true ? "region configured" : "region missing",
        settings.object_storage_force_path_style === true ? "path style" : "virtual hosted",
        settings.object_storage_auto_create_bucket === true ? "auto create" : "manual bucket"
      ].join(" - ")
    ]
  ];
}

export function backupContentValidationSummary(
  validation: unknown,
  scopeLabel: string
): string {
  if (!validation || typeof validation !== "object") {
    return "";
  }
  const details = validation as Record<string, unknown>;
  const status = details.validated === true ? "validated" : formatSettingsLabel(details.status);
  const schema =
    typeof details.schema_version === "string" && details.schema_version.trim().length > 0
      ? details.schema_version.trim()
      : "";
  const expected = formatCount(details.expected_count);
  const archived = formatCount(details.archive_count);
  const checksums = formatCount(details.checksum_verified_count);
  const sizes = formatCount(details.size_verified_count);
  return [
    scopeLabel,
    status,
    schema,
    expected ? `${expected} expected` : "",
    archived ? `${archived} archived` : "",
    checksums ? `${checksums} checksums` : "",
    sizes ? `${sizes} sizes` : ""
  ]
    .filter(Boolean)
    .join(" - ");
}

export function workflowRetryEvidenceSummary(details: Record<string, unknown>): string {
  const total = formatCount(details.total_retry_entries);
  const historical = formatCount(details.historical_retry_entries);
  const resolved = formatCount(details.resolved_retry_entries);
  const scheduled = formatCount(details.scheduled_retry_entries);
  const exhausted = formatCount(details.exhausted_retry_entries);
  const due = formatCount(details.due_retry_entries);
  const backoff = formatCount(details.backoff_retry_entries);
  const resolutionStatus = formatNumberMap(details.by_resolution_status);
  const resolutionStage = formatNumberMap(details.by_resolution_stage);
  return [
    total ? `${total} active` : "",
    historical ? `${historical} historical` : "",
    resolved ? `${resolved} resolved` : "",
    scheduled ? `${scheduled} scheduled` : "",
    due ? `${due} due` : "",
    backoff ? `${backoff} backoff` : "",
    exhausted ? `${exhausted} exhausted` : "",
    resolutionStatus ? `resolved ${resolutionStatus}` : "",
    resolutionStage ? `resolved stages ${resolutionStage}` : ""
  ]
    .filter(Boolean)
    .join(" - ");
}

export function completionReadinessCheckSummary(check: string): string {
  const summaries: Record<string, string> = {
    canonical_transcript_missing: "Create and approve a canonical broadcast transcript.",
    canonical_transcript_not_approved:
      "Approve the canonical broadcast transcript before delivery completion.",
    discussion_structure_qc_missing:
      "Run discussion structure QC before delivery completion.",
    discussion_structure_qc_failing:
      "Resolve missing required topic coverage before approving delivery completion.",
    playable_turns_missing: "The canonical transcript must contain at least one playable turn.",
    character_profile_missing: "Restore or assign the missing speaker participant profile.",
    character_model_missing:
      "Assign a model endpoint and model ID to every playable speaker.",
    character_model_turn_stale:
      "Regenerate turns produced with an older speaker model assignment.",
    character_voice_missing: "Assign a voice profile to every playable speaker.",
    character_voice_asset_stale:
      "Regenerate speech produced with an older speaker voice assignment.",
    character_visual_missing: "Assign a visual profile to every playable speaker.",
    character_visual_asset_stale:
      "Regenerate character visuals produced with an older visual assignment.",
    completed_audio_missing: "Generate completed speech for every playable transcript turn.",
    completed_character_visual_missing:
      "Generate completed primary visuals for every playable transcript turn.",
    shot_planned_reaction_loop_missing:
      "Generate and link completed reusable reaction loops for shot-planned character segments.",
    shot_planned_studio_scene_missing:
      "Generate and link the completed reusable studio scene for shot-planned segments.",
    localized_output_missing: "Create every configured localized transcript.",
    localized_output_not_approved:
      "Approve every configured localized transcript before delivery completion.",
    localized_output_qc_missing:
      "Run semantic-fidelity QC for every configured localized transcript.",
    localized_output_qc_failing:
      "Resolve failing localized transcript semantic-fidelity QC before completion.",
    subtitle_asset_missing: "Generate subtitles for the canonical transcript before completion.",
    timeline_asset_missing: "Build the transcript timeline before completion.",
    timeline_segments_missing: "Ensure the timeline has a segment for every playable turn.",
    audio_qc_missing: "Run speech media integrity QC before delivery completion.",
    audio_qc_failing: "Resolve failing speech media QC before completion.",
    visual_qc_missing: "Run visual media integrity QC before delivery completion.",
    visual_qc_failing: "Resolve failing visual media QC before completion.",
    subtitle_qc_missing: "Run subtitle synchronization QC before delivery completion.",
    subtitle_qc_failing: "Resolve failing subtitle synchronization QC before completion.",
    timeline_qc_missing: "Run timeline integrity QC before delivery completion.",
    timeline_qc_failing: "Resolve failing timeline integrity QC before completion.",
    preview_render_missing: "Render a preview from the approved transcript timeline.",
    preview_render_qc_missing: "Run preview render QC before final delivery completion.",
    preview_render_qc_failing: "Resolve failing preview render QC before completion.",
    preview_render_source_assets_stale:
      "Regenerate the preview render after source timeline or media changes.",
    preview_render_approval_missing: "Approve the preview render before final delivery.",
    final_render_timeline_mismatch:
      "Render the final video from the canonical transcript timeline.",
    research_evidence_pack_missing:
      "Build the required research evidence pack before completion.",
    research_evidence_pack_qc_missing:
      "Run evidence-pack integrity QC before completion.",
    research_evidence_pack_qc_failing:
      "Resolve failing evidence-pack QC before completion.",
    research_approval_missing:
      "Approve the research evidence pack before completion.",
    research_approval_rejected:
      "Rebuild or revise the research evidence pack after review rejection.",
    claim_qc_missing: "Run claim/citation QC for the approved transcript before completion.",
    claim_qc_failing: "Resolve blocking claim/citation QC before completion.",
    completed_final_render_missing: "Render a final video before delivery completion.",
    final_render_qc_missing: "Run final render QC before delivery completion.",
    final_render_qc_failing: "Resolve failing final render QC before completion.",
    final_render_source_assets_stale:
      "Regenerate the final render after source timeline or media changes.",
    final_render_approval_missing: "Approve the latest final render before packaging completion.",
    thumbnail_missing: "Generate a thumbnail from the approved final render.",
    thumbnail_qc_missing: "Run thumbnail integrity QC before delivery completion.",
    thumbnail_qc_failing: "Resolve failing thumbnail QC before completion.",
    completed_export_package_missing: "Create a delivery package linked to the final render.",
    export_package_qc_missing: "Run package integrity QC before delivery completion.",
    export_package_qc_failing: "Resolve failing package integrity QC before completion.",
    export_package_thumbnail_missing:
      "Regenerate the YouTube package so it includes the checked thumbnail.",
    export_package_subtitles_missing:
      "Regenerate the YouTube package so it includes subtitles/captions.",
    completed_production_manifest_missing: "Create the package-linked production manifest.",
    production_manifest_invalid: "Regenerate a valid package-linked production manifest.",
    production_manifest_publish_evidence_missing:
      "Refresh the production manifest so publish job and QC evidence are embedded.",
    publish_job_missing: "Run the publishing handoff before delivery completion.",
    publish_job_not_completed: "Wait for the publishing handoff to complete before closeout.",
    publish_delivery_qc_missing: "Run publish delivery QC before delivery completion.",
    publish_delivery_qc_failing: "Resolve blocking publish delivery QC before closeout.",
    unresolved_failed_assets_present:
      "Replace or regenerate failed media assets; completed manual replacements resolve this gate.",
    failing_quality_results_present: "Resolve failing QC rows before completion."
  };
  return summaries[check] ?? "Resolve this completion gate before marking the run completed.";
}

export function completionReadinessIssueSummary(issue: Record<string, unknown>): string {
  const parts = [
    stringValue(issue.asset_type) || stringValue(issue.check_type) || stringValue(issue.issue),
    stringValue(issue.status),
    stringValue(issue.severity),
    issue.blocks_completion === true
      ? "blocks completion"
      : issue.blocks_completion === false
        ? "nonblocking"
        : "",
    issue.target_type && issue.target_id
      ? `${stringValue(issue.target_type)} ${shortEvidenceId(stringValue(issue.target_id))}`
      : "",
    issue.asset_id ? `asset ${shortEvidenceId(stringValue(issue.asset_id))}` : "",
    issue.quality_result_id
      ? `qc ${shortEvidenceId(stringValue(issue.quality_result_id))}`
      : ""
  ].filter(Boolean);
  return parts.length > 0 ? parts.join(" - ") : "issue details unavailable";
}

export function completionReadinessIssueLabel(
  issue: Record<string, unknown>,
  fallback: string
): string {
  return (
    stringValue(issue.check_type) ||
    stringValue(issue.asset_type) ||
    stringValue(issue.issue) ||
    fallback
  );
}

export function publishJobsEvidenceSummary(details: Record<string, unknown>): string {
  const total = nestedNumber(details, "publish_jobs");
  const submitted = nestedNumber(details, "submitted_publish_jobs");
  const completed = nestedNumber(details, "completed_publish_jobs");
  const failed = nestedNumber(details, "failed_publish_jobs");
  const dryRun = nestedNumber(details, "dry_run_publish_jobs");
  const live = nestedNumber(details, "live_publish_jobs");
  const packages = nestedNumber(details, "completed_export_packages");
  const manifests = nestedNumber(details, "production_manifest_assets");
  const invalidManifests = nestedNumber(details, "invalid_production_manifest_assets");
  const missingManifests = nestedNumber(details, "packages_missing_production_manifest");
  const missingPackageQc = nestedNumber(details, "packages_missing_package_qc");
  const failingPackageQc = nestedNumber(details, "packages_failing_package_qc");
  const missingThumbnails = nestedNumber(details, "packages_missing_thumbnail");
  const missingSubtitles = nestedNumber(details, "packages_missing_subtitles");
  const attention = nestedNumber(details, "attention_count");
  const failedTarget = objectString(details.latest_failed_job, "publisher_target_id");
  const submittedTarget = objectString(details.latest_submitted_job, "publisher_target_id");
  const missingManifestPackageId = objectString(
    details.latest_package_missing_production_manifest,
    "package_asset_id"
  );
  const missingPackageQcId = objectString(
    details.latest_package_missing_package_qc,
    "package_asset_id"
  );
  const failingPackageQcId = objectString(
    details.latest_package_failing_package_qc,
    "package_asset_id"
  );
  const missingThumbnailPackageId = objectString(
    details.latest_package_missing_thumbnail,
    "package_asset_id"
  );
  const missingSubtitlePackageId = objectString(
    details.latest_package_missing_subtitles,
    "package_asset_id"
  );
  const invalidManifestPackageId = objectString(
    details.latest_invalid_production_manifest,
    "package_asset_id"
  );
  const invalidManifestAssetId = objectString(
    details.latest_invalid_production_manifest,
    "manifest_asset_id"
  );
  const invalidManifestReason = productionManifestInvalidReasonSummary(
    objectString(details.latest_invalid_production_manifest, "reason")
  );
  const failedChecks = Array.isArray(details.failed_readiness_checks)
    ? details.failed_readiness_checks.filter((value): value is string => typeof value === "string")
    : [];
  const checks =
    details.readiness_checks && typeof details.readiness_checks === "object"
      ? formatBooleanBreakdown(details.readiness_checks as Record<string, boolean>)
      : "";
  const policy = typeof details.live_readiness_policy === "string" ? details.live_readiness_policy : "";
  return [
    `${total} total`,
    `${submitted} submitted`,
    `${completed} completed`,
    `${failed} failed`,
    `${dryRun} dry-run`,
    `${live} live`,
    `${packages} packages`,
    `${manifests} manifests`,
    `${invalidManifests} invalid manifests`,
    `${missingManifests} missing manifests`,
    `${missingPackageQc} missing package QC`,
    `${failingPackageQc} failing package QC`,
    `${missingThumbnails} missing thumbnails`,
    `${missingSubtitles} missing subtitles`,
    `${attention} attention`,
    failedTarget ? `latest failed ${failedTarget}` : "",
    submittedTarget ? `latest submitted ${submittedTarget}` : "",
    missingManifestPackageId ? `missing manifest ${missingManifestPackageId.slice(0, 8)}` : "",
    missingPackageQcId ? `missing package QC ${missingPackageQcId.slice(0, 8)}` : "",
    failingPackageQcId ? `failing package QC ${failingPackageQcId.slice(0, 8)}` : "",
    missingThumbnailPackageId ? `missing thumbnail ${missingThumbnailPackageId.slice(0, 8)}` : "",
    missingSubtitlePackageId ? `missing subtitles ${missingSubtitlePackageId.slice(0, 8)}` : "",
    invalidManifestPackageId || invalidManifestAssetId
      ? `invalid manifest ${(invalidManifestPackageId || invalidManifestAssetId).slice(0, 8)}`
      : "",
    invalidManifestReason ? `invalid manifest reason ${invalidManifestReason}` : "",
    failedChecks.length ? `failed gates ${failedChecks.slice(0, 4).join("+")}` : "",
    checks,
    policy
  ]
    .filter(Boolean)
    .join(" - ");
}

function stringValue(value: unknown): string {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : "";
}

function shortEvidenceId(value: string): string {
  return value.length > 8 ? value.slice(0, 8) : value;
}

export function productionManifestInvalidReasonSummary(reason: string): string {
  const normalized = reason.trim();
  if (!normalized) {
    return "";
  }
  const summaries: Record<string, string> = {
    "embedded production_manifest is missing": "manifest payload missing",
    "embedded production_manifest schema_version is invalid": "manifest schema invalid",
    "embedded delivery package asset_id is missing": "package link missing",
    "embedded delivery package asset_id does not match source_entity_id":
      "package link mismatches manifest source",
    "embedded delivery package asset_id does not match package asset":
      "package link mismatches selected package",
    "embedded delivery package checksum does not match package asset":
      "package checksum is stale",
    "embedded delivery package storage_uri does not match package asset":
      "package storage is stale",
    "embedded delivery package package_id does not match package asset":
      "package generation ID is stale",
    "embedded delivery package chapters do not match timeline chapters":
      "package chapters are stale",
    "embedded production manifest timeline chapters are missing":
      "timeline chapters missing",
    "embedded delivery package manifest is missing": "package manifest missing",
    "embedded talkshow visual handoff is missing": "talk-show visual handoff missing",
    "embedded talkshow visual handoff schema_version is invalid":
      "talk-show visual handoff schema invalid",
    "reusable talk-show visual handoff is incomplete":
      "reusable talk-show visual handoff incomplete",
    "embedded delivery package thumbnail does not match thumbnail asset":
      "package thumbnail link is stale",
    "embedded delivery package thumbnail file is missing": "package thumbnail file missing",
    "embedded delivery package subtitle file is missing": "package subtitle file missing",
    "embedded delivery package subtitle manifest is missing": "package subtitle manifest missing"
  };
  return summaries[normalized] ?? "manifest structure invalid";
}

export function deploymentProviderEvidenceSummary(
  details: Record<string, unknown>,
): string {
  return [
    deploymentProviderSummary(details.model_provider_summary, "models"),
    deploymentProviderSummary(details.voicebox_summary, "voicebox"),
    deploymentProviderSummary(details.comfyui_summary, "comfyui"),
    deploymentPublisherTargetSummary(details.publisher_target_summary)
  ]
    .filter(Boolean)
    .join(" - ");
}

export function managedMediaSmokeReadinessSummary(details: Record<string, unknown>): string {
  const status = objectString(details, "status") || "unknown";
  if (status === "not_configured") {
    return "not configured";
  }
  const artifactCount = formatNumber(details.artifact_count);
  const ageSeconds = nestedNumber(details, "age_seconds");
  return [
    status,
    details.ready === true ? "ready" : details.ready === false ? "not ready" : "",
    details.fresh === false ? "stale" : details.fresh === true ? "current" : "",
    ageSeconds ? `${ageSeconds}s old` : "",
    objectString(details, "model"),
    objectString(details, "operation"),
    objectString(details, "terminal_state"),
    objectString(details, "terminal_stage"),
    objectString(details, "failure_category")
      ? `failure ${objectString(details, "failure_category")}`
      : "",
    artifactCount ? `${artifactCount} artifacts` : "",
    managedMediaSmokeActionLabel(objectString(details, "action")),
    formatFailedChecks(details.failed_readiness_checks)
  ]
    .filter(Boolean)
    .join(" - ");
}

export function modelGenerationObservabilitySummary(details: Record<string, unknown>): string {
  const turns = nestedNumber(details, "turn_count");
  const latency = nestedNumber(details, "latency_recorded_turn_count");
  const tokenUsage = nestedNumber(details, "token_usage_recorded_turn_count");
  const averageLatency = formatNumber(details.average_model_latency_ms);
  const totalTokens = nestedNumber(details, "total_tokens");
  const byProvider = formatNestedCountMap(details.by_provider_type, "turn_count");
  const failedChecks = formatFailedChecks(details.failed_readiness_checks);
  const checks =
    details.readiness_checks && typeof details.readiness_checks === "object"
      ? formatBooleanBreakdown(details.readiness_checks as Record<string, boolean>)
      : "";
  return [
    `${turns} turns`,
    `${latency} latency records`,
    `${tokenUsage} token records`,
    averageLatency ? `${averageLatency} ms avg` : "",
    `${totalTokens} tokens`,
    byProvider ? `providers ${byProvider}` : "",
    failedChecks,
    checks
  ]
    .filter(Boolean)
    .join(" - ");
}

export function assetProductionObservabilitySummary(details: Record<string, unknown>): string {
  const assets = nestedNumber(details, "asset_count");
  const completed = nestedNumber(details, "completed_asset_count");
  const failed = nestedNumber(details, "failed_asset_count");
  const durations = nestedNumber(details, "duration_recorded_asset_count");
  const sizes = nestedNumber(details, "size_recorded_asset_count");
  const failureRate = formatPercent(details.failure_rate);
  const byType = formatNestedCountMap(details.by_asset_type, "asset_count");
  const byLanguage = formatNestedCountMap(details.by_language, "asset_count");
  return [
    `${assets} assets`,
    `${completed} completed`,
    `${failed} failed`,
    `${durations} duration records`,
    `${sizes} size records`,
    failureRate ? `${failureRate} failure` : "",
    byType ? `types ${byType}` : "",
    byLanguage ? `languages ${byLanguage}` : ""
  ]
    .filter(Boolean)
    .join(" - ");
}

export function workflowDurationObservabilitySummary(details: Record<string, unknown>): string {
  const productionRecords = nestedNumber(details, "production_duration_record_count");
  const stageRecords = nestedNumber(details, "stage_duration_record_count");
  const productionMs = nestedNumber(details, "production_duration_ms_sum");
  const stageMs = nestedNumber(details, "stage_duration_ms_sum");
  const byStage = formatNestedCountMap(details.by_stage, "duration_record_count");
  const byLanguage = formatNestedCountMap(details.by_language, "duration_record_count");
  return [
    `${productionRecords} run records`,
    `${stageRecords} stage records`,
    productionMs ? `${productionMs} ms runs` : "",
    stageMs ? `${stageMs} ms stages` : "",
    byStage ? `stages ${byStage}` : "",
    byLanguage ? `languages ${byLanguage}` : ""
  ]
    .filter(Boolean)
    .join(" - ");
}

export function queueWaitObservabilitySummary(details: Record<string, unknown>): string {
  const pendingRecords = nestedNumber(details, "pending_wait_record_count");
  const completedRecords = nestedNumber(details, "completed_wait_record_count");
  const pendingMs = nestedNumber(details, "pending_wait_ms_sum");
  const completedMs = nestedNumber(details, "completed_wait_ms_sum");
  const byQueuePending = formatNestedCountMap(details.by_queue, "pending_wait_record_count");
  const byQueueCompleted = formatNestedCountMap(details.by_queue, "completed_wait_record_count");
  const byLanguagePending = formatNestedCountMap(
    details.by_language,
    "pending_wait_record_count"
  );
  const byLanguageCompleted = formatNestedCountMap(
    details.by_language,
    "completed_wait_record_count"
  );
  return [
    `${pendingRecords} pending records`,
    `${completedRecords} completed records`,
    pendingMs ? `${pendingMs} ms pending` : "",
    completedMs ? `${completedMs} ms completed` : "",
    byQueuePending ? `pending queues ${byQueuePending}` : "",
    byQueueCompleted ? `completed queues ${byQueueCompleted}` : "",
    byLanguagePending ? `pending languages ${byLanguagePending}` : "",
    byLanguageCompleted ? `completed languages ${byLanguageCompleted}` : ""
  ]
    .filter(Boolean)
    .join(" - ");
}

function deploymentProviderSummary(value: unknown, label: string): string {
  if (!value || typeof value !== "object") {
    return "";
  }
  const summary = value as Record<string, unknown>;
  const configured = formatCount(summary.configured);
  const enabled = formatCount(summary.enabled);
  const remote = formatPositiveCount(summary.remote_enabled);
  const missingBaseUrl = formatPositiveCount(summary.missing_base_url);
  const unhealthy = formatPositiveCount(summary.unhealthy);
  const unknown = formatPositiveCount(summary.unknown);
  const parts = [
    configured ? `${configured} configured` : "",
    enabled ? `${enabled} enabled` : "",
    remote ? `${remote} remote` : "",
    missingBaseUrl ? `${missingBaseUrl} missing url` : "",
    unhealthy ? `${unhealthy} unhealthy` : "",
    unknown ? `${unknown} unknown` : ""
  ].filter(Boolean);
  return parts.length ? `${label} ${parts.join("/")}` : "";
}

function managedMediaSmokeActionLabel(action: string): string {
  if (action === "managed_media_smoke_ready") {
    return "smoke ready";
  }
  if (action === "fix_b1_managed_media_runner_then_rerun_smoke") {
    return "fix B1 runner";
  }
  if (action === "run_b1_managed_media_smoke") {
    return "run media smoke";
  }
  if (action === "wait_for_b1_media_capacity_then_rerun_smoke") {
    return "B1 busy, retry later";
  }
  if (action === "inspect_b1_managed_media_smoke") {
    return "inspect media smoke";
  }
  return "";
}

function deploymentPublisherTargetSummary(value: unknown): string {
  if (!value || typeof value !== "object") {
    return "";
  }
  const summary = value as Record<string, unknown>;
  const configured = formatCount(summary.configured);
  const enabled = formatCount(summary.enabled);
  const liveEnabled = formatPositiveCount(summary.live_enabled);
  const unhealthy = formatPositiveCount(summary.unhealthy);
  const unknown = formatPositiveCount(summary.unknown);
  const parts = [
    configured ? `${configured} configured` : "",
    enabled ? `${enabled} enabled` : "",
    liveEnabled ? `${liveEnabled} live` : "",
    unhealthy ? `${unhealthy} unhealthy` : "",
    unknown ? `${unknown} unknown` : ""
  ].filter(Boolean);
  return parts.length ? `publishers ${parts.join("/")}` : "";
}

function hasAuthSettings(settings: Record<string, unknown>): boolean {
  return Object.keys(settings).some((key) => key.startsWith("auth_"));
}

function hasObjectStorageSettings(settings: Record<string, unknown>): boolean {
  return Object.keys(settings).some((key) => key.startsWith("object_storage_"));
}

function formatSettingsLabel(value: unknown): string {
  return typeof value === "string" && value.trim().length > 0 ? value.trim() : "blank";
}

function formatCount(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}

function formatPositiveCount(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) && value > 0
    ? String(value)
    : "";
}

function formatNumber(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value) ? String(value) : "";
}

function formatPercent(value: unknown): string {
  return typeof value === "number" && Number.isFinite(value)
    ? `${Math.round(value * 1000) / 10}%`
    : "";
}

function formatFailedChecks(value: unknown): string {
  if (!Array.isArray(value)) {
    return "";
  }
  const checks = value.filter((item): item is string => typeof item === "string");
  return checks.length ? `failed gates ${checks.slice(0, 4).join("+")}` : "";
}

function nestedNumber(value: unknown, key: string): number {
  if (!value || typeof value !== "object") {
    return 0;
  }
  const raw = (value as Record<string, unknown>)[key];
  return typeof raw === "number" && Number.isFinite(raw) ? raw : 0;
}

function objectString(value: unknown, key: string): string {
  if (!value || typeof value !== "object") {
    return "";
  }
  const raw = (value as Record<string, unknown>)[key];
  return typeof raw === "string" ? raw : "";
}

function formatNestedCountMap(value: unknown, countKey: string): string {
  if (!value || typeof value !== "object") {
    return "";
  }
  return Object.entries(value as Record<string, unknown>)
    .sort(([left], [right]) => left.localeCompare(right))
    .filter((entry): entry is [string, Record<string, unknown>] => {
      const [, item] = entry;
      return item !== null && typeof item === "object";
    })
    .map(([name, item]) => {
      const count = nestedNumber(item, countKey);
      return count > 0 ? `${name}:${count}` : "";
    })
    .filter(Boolean)
    .join(",");
}

function formatNumberMap(value: unknown): string {
  if (!value || typeof value !== "object") {
    return "";
  }
  return Object.entries(value as Record<string, unknown>)
    .filter(([, count]) => typeof count === "number" && Number.isFinite(count))
    .sort(([left], [right]) => left.localeCompare(right))
    .map(([key, count]) => `${key}:${count}`)
    .join(", ");
}

function formatBooleanBreakdown(value: Record<string, boolean>): string {
  const entries = Object.entries(value)
    .filter((entry): entry is [string, boolean] => typeof entry[1] === "boolean")
    .sort(([left], [right]) => left.localeCompare(right));
  if (entries.length === 0) {
    return "";
  }
  return entries.map(([key, ready]) => `${key}: ${ready ? "ok" : "fail"}`).join(" - ");
}
