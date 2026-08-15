import { describe, expect, it } from "vitest";
import {
  authSettingsDetailRows,
  assetProductionObservabilitySummary,
  backupContentValidationSummary,
  completionReadinessIssueLabel,
  completionReadinessIssueSummary,
  completionReadinessCheckSummary,
  deploymentProviderEvidenceSummary,
  managedMediaSmokeReadinessSummary,
  modelGenerationObservabilitySummary,
  objectStorageSettingsDetailRows,
  productionManifestInvalidReasonSummary,
  publishJobsEvidenceSummary,
  queueWaitObservabilitySummary,
  workflowDurationObservabilitySummary,
  workflowRetryEvidenceSummary
} from "./systemHealthEvidence";

describe("authSettingsDetailRows", () => {
  it("returns no rows before health settings are loaded", () => {
    expect(authSettingsDetailRows({})).toEqual([]);
  });

  it("returns no rows for settings payloads without auth evidence", () => {
    expect(
      authSettingsDetailRows({
        environment: "production",
        object_storage_backend: "s3",
        publisher_automated_live_enabled: true
      })
    ).toEqual([]);
  });

  it("formats safe auth settings without exposing secret values", () => {
    const rows = authSettingsDetailRows({
      auth_enabled: true,
      auth_api_key_reference_configured: true,
      auth_role_header: "x-role",
      auth_user_header: "x-user",
      auth_trusted_identity_enabled: true,
      auth_trusted_identity_header: "x-forwarded-user",
      auth_trusted_email_header: "x-forwarded-email",
      auth_trusted_groups_header: "x-forwarded-groups",
      auth_trusted_default_role: "viewer",
      auth_trusted_group_role_map_configured: true,
      auth_provider_session_enabled: true,
      auth_provider_session_introspection_configured: true,
      auth_provider_session_client_id_reference_configured: true,
      auth_provider_session_client_secret_reference_configured: false,
      auth_provider_session_token_header: "authorization",
      auth_provider_session_user_claim: "sub",
      auth_provider_session_groups_claim: "groups",
      auth_provider_session_default_role: "producer",
      auth_provider_session_group_role_map_configured: true,
      auth_api_key_reference: "env:SHOULD_NOT_RENDER",
      auth_provider_session_client_secret_reference: "env:SHOULD_NOT_RENDER"
    });

    expect(rows).toEqual([
      ["Auth Mode", "enabled - api-key reference"],
      ["API Key Headers", "role x-role - user x-user"],
      [
        "Trusted Identity",
        "enabled - identity x-forwarded-user - email x-forwarded-email - groups x-forwarded-groups - default viewer - group roles"
      ],
      [
        "Provider Sessions",
        "enabled - introspection - client id ref - no client secret ref"
      ],
      [
        "Provider Claims",
        "token authorization - user sub - groups groups - default producer - group roles"
      ]
    ]);
    const rendered = rows.map(([, value]) => value).join("\n");
    expect(rendered).not.toContain("SHOULD_NOT_RENDER");
    expect(rendered).not.toContain("env:");
  });

  it("marks blank header and claim labels explicitly", () => {
    const rows = authSettingsDetailRows({
      auth_enabled: true,
      auth_api_key_reference_configured: false,
      auth_role_header: "",
      auth_user_header: " ",
      auth_trusted_identity_enabled: false,
      auth_trusted_identity_header: null,
      auth_trusted_email_header: undefined,
      auth_trusted_groups_header: "",
      auth_trusted_default_role: " ",
      auth_provider_session_enabled: false,
      auth_provider_session_introspection_configured: false,
      auth_provider_session_token_header: "",
      auth_provider_session_user_claim: " ",
      auth_provider_session_groups_claim: null,
      auth_provider_session_default_role: undefined
    });

    expect(rows).toContainEqual(["Auth Mode", "enabled - api-key missing"]);
    expect(rows).toContainEqual(["API Key Headers", "role blank - user blank"]);
    expect(rows).toContainEqual([
      "Provider Claims",
      "token blank - user blank - groups blank - default blank - no group roles"
    ]);
  });
});

describe("objectStorageSettingsDetailRows", () => {
  it("returns no rows before storage settings are loaded", () => {
    expect(objectStorageSettingsDetailRows({})).toEqual([]);
  });

  it("formats safe object-storage settings without exposing endpoints or secret refs", () => {
    const rows = objectStorageSettingsDetailRows({
      object_storage_backend: "s3",
      object_storage_bucket: "dialecticore-private",
      object_storage_bucket_configured: true,
      object_storage_endpoint: "https://minio.internal:9000",
      object_storage_endpoint_configured: true,
      object_storage_region_configured: true,
      object_storage_access_key_reference: "docker-secret:minio_access_key",
      object_storage_secret_key_reference: "docker-secret:minio_secret_key",
      object_storage_access_key_reference_configured: true,
      object_storage_secret_key_reference_configured: true,
      object_storage_credential_pair_configured: true,
      object_storage_force_path_style: true,
      object_storage_auto_create_bucket: false
    });

    expect(rows).toEqual([
      ["Object Storage Settings", "s3 - bucket configured - endpoint configured"],
      ["Object Storage Credentials", "paired - access ref - secret ref"],
      ["Object Storage Options", "region configured - path style - manual bucket"]
    ]);
    const rendered = rows.map(([, value]) => value).join("\n");
    expect(rendered).not.toContain("minio.internal");
    expect(rendered).not.toContain("docker-secret:");
    expect(rendered).not.toContain("dialecticore-private");
  });
});

describe("backupContentValidationSummary", () => {
  it("formats safe backup content validation counts", () => {
    expect(
      backupContentValidationSummary(
        {
          validated: true,
          status: "validated",
          schema_version: "file_storage_restore_validation.v1",
          expected_count: 2,
          archive_count: 2,
          checksum_verified_count: 2,
          size_verified_count: 2,
          unsafe_path: "object-storage/audio/private.wav"
        },
        "objects"
      )
    ).toBe(
      "objects - validated - file_storage_restore_validation.v1 - 2 expected - 2 archived - 2 checksums - 2 sizes"
    );
  });

  it("returns no content validation summary when evidence is absent", () => {
    expect(backupContentValidationSummary(null, "runtime")).toBe("");
    expect(backupContentValidationSummary("missing", "runtime")).toBe("");
  });

  it("does not render raw archive paths or object keys from unexpected fields", () => {
    const summary = backupContentValidationSummary(
      {
        validated: false,
        status: "missing",
        path: "runtime-state/workers/worker.json",
        key: "audio/private.wav"
      },
      "runtime"
    );

    expect(summary).toBe("runtime - missing");
    expect(summary).not.toContain("worker.json");
    expect(summary).not.toContain("private.wav");
  });
});

describe("workflowRetryEvidenceSummary", () => {
  it("formats active backlog separately from resolved retry history", () => {
    expect(
      workflowRetryEvidenceSummary({
        total_retry_entries: 3,
        historical_retry_entries: 4,
        resolved_retry_entries: 1,
        scheduled_retry_entries: 2,
        due_retry_entries: 1,
        backoff_retry_entries: 1,
        exhausted_retry_entries: 1,
        by_resolution_status: { operator_retried: 1 },
        by_resolution_stage: { voicebox: 1 }
      })
    ).toBe(
      "3 active - 4 historical - 1 resolved - 2 scheduled - 1 due - 1 backoff - 1 exhausted - resolved operator_retried:1 - resolved stages voicebox:1"
    );
  });

  it("does not render actor or signal details from retry resolution history", () => {
    const summary = workflowRetryEvidenceSummary({
      total_retry_entries: 0,
      historical_retry_entries: 1,
      resolved_retry_entries: 1,
      resolved_by: "producer@example.test",
      resolution_signal_id: "signal-secret",
      by_resolution_status: { manual_edit_resolved: 1 }
    });

    expect(summary).toContain("1 historical");
    expect(summary).toContain("1 resolved");
    expect(summary).not.toContain("producer@example.test");
    expect(summary).not.toContain("signal-secret");
  });
});

describe("completionReadinessCheckSummary", () => {
  it("explains package QC and production manifest completion blockers", () => {
    expect(completionReadinessCheckSummary("canonical_transcript_missing")).toBe(
      "Create and approve a canonical broadcast transcript."
    );
    expect(completionReadinessCheckSummary("canonical_transcript_not_approved")).toBe(
      "Approve the canonical broadcast transcript before delivery completion."
    );
    expect(completionReadinessCheckSummary("discussion_structure_qc_failing")).toBe(
      "Resolve missing required topic coverage before approving delivery completion."
    );
    expect(completionReadinessCheckSummary("completed_audio_missing")).toBe(
      "Generate completed speech for every playable transcript turn."
    );
    expect(completionReadinessCheckSummary("completed_character_visual_missing")).toBe(
      "Generate completed primary visuals for every playable transcript turn."
    );
    expect(completionReadinessCheckSummary("character_model_missing")).toBe(
      "Assign a model endpoint and model ID to every playable speaker."
    );
    expect(completionReadinessCheckSummary("character_model_turn_stale")).toBe(
      "Regenerate turns produced with an older speaker model assignment."
    );
    expect(completionReadinessCheckSummary("character_voice_asset_stale")).toBe(
      "Regenerate speech produced with an older speaker voice assignment."
    );
    expect(completionReadinessCheckSummary("shot_planned_reaction_loop_missing")).toBe(
      "Generate and link completed reusable reaction loops for shot-planned character segments."
    );
    expect(completionReadinessCheckSummary("shot_planned_studio_scene_missing")).toBe(
      "Generate and link the completed reusable studio scene for shot-planned segments."
    );
    expect(completionReadinessCheckSummary("localized_output_not_approved")).toBe(
      "Approve every configured localized transcript before delivery completion."
    );
    expect(completionReadinessCheckSummary("localized_output_qc_failing")).toBe(
      "Resolve failing localized transcript semantic-fidelity QC before completion."
    );
    expect(completionReadinessCheckSummary("timeline_segments_missing")).toBe(
      "Ensure the timeline has a segment for every playable turn."
    );
    expect(completionReadinessCheckSummary("audio_qc_missing")).toBe(
      "Run speech media integrity QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("visual_qc_missing")).toBe(
      "Run visual media integrity QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("subtitle_qc_missing")).toBe(
      "Run subtitle synchronization QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("timeline_qc_missing")).toBe(
      "Run timeline integrity QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("preview_render_missing")).toBe(
      "Render a preview from the approved transcript timeline."
    );
    expect(completionReadinessCheckSummary("preview_render_approval_missing")).toBe(
      "Approve the preview render before final delivery."
    );
    expect(completionReadinessCheckSummary("preview_render_source_assets_stale")).toBe(
      "Regenerate the preview render after source timeline or media changes."
    );
    expect(completionReadinessCheckSummary("final_render_timeline_mismatch")).toBe(
      "Render the final video from the canonical transcript timeline."
    );
    expect(completionReadinessCheckSummary("final_render_qc_missing")).toBe(
      "Run final render QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("final_render_qc_failing")).toBe(
      "Resolve failing final render QC before completion."
    );
    expect(completionReadinessCheckSummary("final_render_source_assets_stale")).toBe(
      "Regenerate the final render after source timeline or media changes."
    );
    expect(completionReadinessCheckSummary("research_evidence_pack_missing")).toBe(
      "Build the required research evidence pack before completion."
    );
    expect(completionReadinessCheckSummary("research_approval_missing")).toBe(
      "Approve the research evidence pack before completion."
    );
    expect(completionReadinessCheckSummary("research_approval_rejected")).toBe(
      "Rebuild or revise the research evidence pack after review rejection."
    );
    expect(completionReadinessCheckSummary("claim_qc_missing")).toBe(
      "Run claim/citation QC for the approved transcript before completion."
    );
    expect(completionReadinessCheckSummary("thumbnail_missing")).toBe(
      "Generate a thumbnail from the approved final render."
    );
    expect(completionReadinessCheckSummary("export_package_thumbnail_missing")).toBe(
      "Regenerate the YouTube package so it includes the checked thumbnail."
    );
    expect(completionReadinessCheckSummary("export_package_subtitles_missing")).toBe(
      "Regenerate the YouTube package so it includes subtitles/captions."
    );
    expect(completionReadinessCheckSummary("export_package_qc_missing")).toBe(
      "Run package integrity QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("export_package_qc_failing")).toBe(
      "Resolve failing package integrity QC before completion."
    );
    expect(completionReadinessCheckSummary("production_manifest_invalid")).toBe(
      "Regenerate a valid package-linked production manifest."
    );
    expect(completionReadinessCheckSummary("production_manifest_publish_evidence_missing")).toBe(
      "Refresh the production manifest so publish job and QC evidence are embedded."
    );
    expect(completionReadinessCheckSummary("publish_job_missing")).toBe(
      "Run the publishing handoff before delivery completion."
    );
    expect(completionReadinessCheckSummary("publish_job_not_completed")).toBe(
      "Wait for the publishing handoff to complete before closeout."
    );
    expect(completionReadinessCheckSummary("publish_delivery_qc_missing")).toBe(
      "Run publish delivery QC before delivery completion."
    );
    expect(completionReadinessCheckSummary("publish_delivery_qc_failing")).toBe(
      "Resolve blocking publish delivery QC before closeout."
    );
    expect(completionReadinessCheckSummary("unresolved_failed_assets_present")).toBe(
      "Replace or regenerate failed media assets; completed manual replacements resolve this gate."
    );
  });

  it("keeps a generic fallback for unknown backend gates", () => {
    expect(completionReadinessCheckSummary("new_backend_gate")).toBe(
      "Resolve this completion gate before marking the run completed."
    );
  });
});

describe("completionReadinessIssueSummary", () => {
  it("summarizes blocking failed assets with short identifiers", () => {
    const summary = completionReadinessIssueSummary({
      asset_type: "audio",
      status: "failed",
      severity: "fail",
      blocks_completion: true,
      asset_id: "b5e91cd5-7671-4304-a780-89019ed16310",
      storage_uri: "object://dialecticore/audio/private.wav"
    });

    expect(summary).toBe("audio - failed - fail - blocks completion - asset b5e91cd5");
    expect(summary).not.toContain("private.wav");
    expect(summary).not.toContain("object://");
  });

  it("summarizes nonblocking failed QC without exposing raw details", () => {
    const summary = completionReadinessIssueSummary({
      check_type: "audio_media_integrity",
      status: "fail",
      severity: "warning",
      blocks_completion: false,
      target_type: "transcript_version",
      target_id: "72a30172-143a-4e8b-a114-94d554cc84d4",
      quality_result_id: "d703f77e-a1d2-4cb7-88f7-4497c218b35f",
      details: {
        credential_reference: "env:OPENROUTER_API_KEY",
        storage_uri: "object://dialecticore/private.wav"
      }
    });

    expect(summary).toBe(
      "audio_media_integrity - fail - warning - nonblocking - transcript_version 72a30172 - qc d703f77e"
    );
    expect(summary).not.toContain("OPENROUTER");
    expect(summary).not.toContain("object://");
  });

  it("selects a stable issue label", () => {
    expect(
      completionReadinessIssueLabel(
        {
          check_type: "render_final_integrity",
          asset_type: "render"
        },
        "fallback"
      )
    ).toBe("render_final_integrity");
    expect(completionReadinessIssueLabel({}, "fallback")).toBe("fallback");
  });
});

describe("publishJobsEvidenceSummary", () => {
  it("formats package QC coverage without exposing raw package evidence", () => {
    const summary = publishJobsEvidenceSummary({
      publish_jobs: 3,
      submitted_publish_jobs: 1,
      completed_publish_jobs: 1,
      failed_publish_jobs: 1,
      dry_run_publish_jobs: 2,
      live_publish_jobs: 1,
      completed_export_packages: 2,
      production_manifest_assets: 1,
      invalid_production_manifest_assets: 1,
      packages_missing_production_manifest: 1,
      packages_missing_package_qc: 1,
      packages_failing_package_qc: 1,
      packages_missing_thumbnail: 1,
      packages_missing_subtitles: 1,
      attention_count: 2,
      latest_failed_job: { publisher_target_id: "youtube-live" },
      latest_submitted_job: { publisher_target_id: "generic-http" },
      latest_package_missing_production_manifest: {
        package_asset_id: "manifest-package-secret-id"
      },
      latest_package_missing_package_qc: {
        package_asset_id: "package-qc-secret-id",
        storage_uri: "s3://private/package.zip"
      },
      latest_package_failing_package_qc: {
        package_asset_id: "failing-qc-secret-id",
        quality_result_id: "quality-secret-id"
      },
      latest_package_missing_thumbnail: {
        package_asset_id: "thumbnail-package-secret-id",
        thumbnail_asset_id: "thumbnail-asset-secret-id"
      },
      latest_package_missing_subtitles: {
        package_asset_id: "subtitle-package-secret-id",
        subtitle_asset_id: "subtitle-asset-secret-id"
      },
      latest_invalid_production_manifest: {
        manifest_asset_id: "manifest-asset-secret-id",
        package_asset_id: "invalid-manifest-package-secret-id",
        storage_uri: "s3://private/production-manifest.json",
        reason: "embedded talkshow visual handoff is missing"
      },
      failed_readiness_checks: [
        "no_packages_missing_package_qc",
        "no_packages_failing_package_qc",
        "no_packages_missing_thumbnails",
        "no_packages_missing_subtitles",
        "no_invalid_production_manifests",
        "no_packages_missing_production_manifest"
      ],
      readiness_checks: {
        no_packages_failing_package_qc: false,
        no_invalid_production_manifests: false,
        no_packages_missing_package_qc: false,
        no_packages_missing_thumbnails: false,
        no_packages_missing_subtitles: false,
        no_packages_missing_production_manifest: false
      },
      live_readiness_policy: "strict"
    });

    expect(summary).toContain("1 missing package QC");
    expect(summary).toContain("1 failing package QC");
    expect(summary).toContain("1 missing thumbnails");
    expect(summary).toContain("1 missing subtitles");
    expect(summary).toContain("1 invalid manifests");
    expect(summary).toContain("missing package QC package-");
    expect(summary).toContain("failing package QC failing-");
    expect(summary).toContain("missing thumbnail thumbnai");
    expect(summary).toContain("missing subtitles subtitle");
    expect(summary).toContain("invalid manifest invalid-");
    expect(summary).toContain("invalid manifest reason talk-show visual handoff missing");
    expect(summary).toContain(
      "failed gates no_packages_missing_package_qc+no_packages_failing_package_qc+no_packages_missing_thumbnails+no_packages_missing_subtitles"
    );
    expect(summary).not.toContain("s3://private");
    expect(summary).not.toContain("package-qc-secret-id");
    expect(summary).not.toContain("failing-qc-secret-id");
    expect(summary).not.toContain("quality-secret-id");
    expect(summary).not.toContain("thumbnail-package-secret-id");
    expect(summary).not.toContain("thumbnail-asset-secret-id");
    expect(summary).not.toContain("subtitle-package-secret-id");
    expect(summary).not.toContain("subtitle-asset-secret-id");
    expect(summary).not.toContain("manifest-package-secret-id");
    expect(summary).not.toContain("manifest-asset-secret-id");
    expect(summary).not.toContain("invalid-manifest-package-secret-id");
  });

  it("summarizes stale manifest package evidence without rendering raw reasons", () => {
    expect(productionManifestInvalidReasonSummary("")).toBe("");
    expect(
      productionManifestInvalidReasonSummary(
        "embedded delivery package checksum does not match package asset"
      )
    ).toBe("package checksum is stale");
    expect(
      productionManifestInvalidReasonSummary(
        "embedded delivery package storage_uri does not match package asset"
      )
    ).toBe("package storage is stale");
    expect(
      productionManifestInvalidReasonSummary(
        "embedded delivery package package_id does not match package asset"
      )
    ).toBe("package generation ID is stale");

    const summary = publishJobsEvidenceSummary({
      invalid_production_manifest_assets: 1,
      latest_invalid_production_manifest: {
        manifest_asset_id: "manifest-secret-id",
        package_asset_id: "package-secret-id",
        reason:
          "embedded delivery package storage_uri does not match package asset object://private/package.zip"
      }
    });

    expect(summary).toContain("1 invalid manifests");
    expect(summary).toContain("invalid manifest package");
    expect(summary).toContain("invalid manifest reason manifest structure invalid");
    expect(summary).not.toContain("object://private");
    expect(summary).not.toContain("package.zip");
    expect(summary).not.toContain("package-secret-id");
    expect(summary).not.toContain("manifest-secret-id");
  });
});

describe("deploymentProviderEvidenceSummary", () => {
  it("formats safe AI and media endpoint deployment posture counts", () => {
    expect(
      deploymentProviderEvidenceSummary({
        model_provider_summary: {
          configured: 2,
          enabled: 2,
          remote_enabled: 1,
          missing_base_url: 1,
          unhealthy: 0,
          unknown: 1,
          endpoint_id: "model-secret"
        },
        voicebox_summary: {
          configured: 1,
          enabled: 1,
          remote_enabled: 0,
          missing_base_url: 0,
          unhealthy: 1,
          unknown: 0,
          base_url: "https://voicebox.internal"
        },
        comfyui_summary: {
          configured: 3,
          enabled: 2,
          remote_enabled: 2,
          missing_base_url: 0,
          unhealthy: 0,
          unknown: 0
        },
        publisher_target_summary: {
          configured: 4,
          enabled: 3,
          live_enabled: 1,
          unhealthy: 0,
          unknown: 1,
          target_id: "publisher-secret"
        }
      })
    ).toBe(
      "models 2 configured/2 enabled/1 remote/1 missing url/1 unknown - voicebox 1 configured/1 enabled/1 unhealthy - comfyui 3 configured/2 enabled/2 remote - publishers 4 configured/3 enabled/1 live/1 unknown"
    );
  });

  it("does not render endpoint identifiers or raw URLs from provider summaries", () => {
    const summary = deploymentProviderEvidenceSummary({
      model_provider_summary: {
        configured: 1,
        enabled: 1,
        remote_enabled: 1,
        endpoint_id: "private-model-endpoint",
        base_url: "https://models.internal"
      },
      publisher_target_summary: {
        configured: 1,
        enabled: 1,
        live_enabled: 1,
        target_id: "private-publisher-target",
        base_url: "https://publisher.internal"
      }
    });

    expect(summary).toBe("models 1 configured/1 enabled/1 remote - publishers 1 configured/1 enabled/1 live");
    expect(summary).not.toContain("private-model-endpoint");
    expect(summary).not.toContain("models.internal");
    expect(summary).not.toContain("private-publisher-target");
    expect(summary).not.toContain("publisher.internal");
  });
});

describe("managedMediaSmokeReadinessSummary", () => {
  it("summarizes managed-media smoke runner failures without file paths or raw errors", () => {
    const summary = managedMediaSmokeReadinessSummary({
      configured: true,
      path: "output/smoke/private-smoke.json",
      status: "runner_failed",
      ready: false,
      model: "image-default",
      operation: "image-generation",
      terminal_state: "failed",
      terminal_stage: "failed",
      failure_category: "gpu_runner_error",
      failure_message: "ValueError with /private/runner/path",
      artifact_count: 0,
      action: "fix_b1_managed_media_runner_then_rerun_smoke",
      failed_readiness_checks: ["managed_media_smoke_passed"]
    });

    expect(summary).toBe(
      "runner_failed - not ready - image-default - image-generation - failed - failed - failure gpu_runner_error - 0 artifacts - fix B1 runner - failed gates managed_media_smoke_passed"
    );
    expect(summary).not.toContain("private-smoke.json");
    expect(summary).not.toContain("/private/runner/path");
    expect(summary).not.toContain("ValueError");
  });

  it("marks unconfigured smoke evidence directly", () => {
    expect(
      managedMediaSmokeReadinessSummary({
        configured: false,
        status: "not_configured",
        ready: null
      })
    ).toBe("not configured");
  });

  it("describes a busy B1 scheduler as a retry, not a runner repair", () => {
    expect(
      managedMediaSmokeReadinessSummary({
        configured: true,
        status: "busy",
        ready: false,
        fresh: true,
        model: "image-default",
        action: "wait_for_b1_media_capacity_then_rerun_smoke",
        failed_readiness_checks: ["managed_media_smoke_passed"]
      })
    ).toBe(
      "busy - not ready - current - image-default - B1 busy, retry later - failed gates managed_media_smoke_passed"
    );
  });
});

describe("observability summaries", () => {
  it("formats model generation observability without raw provider evidence", () => {
    const summary = modelGenerationObservabilitySummary({
      turn_count: 3,
      latency_recorded_turn_count: 2,
      token_usage_recorded_turn_count: 1,
      average_model_latency_ms: 42.5,
      total_tokens: 99,
      by_provider_type: {
        openai_compatible: { turn_count: 2, base_url: "https://models.internal" },
        mock: { turn_count: 1, api_key: "secret-token" }
      },
      failed_readiness_checks: ["model_latency_recorded_for_turns"],
      readiness_checks: {
        model_latency_recorded_for_turns: false,
        token_usage_aggregation_available: true
      }
    });

    expect(summary).toBe(
      "3 turns - 2 latency records - 1 token records - 42.5 ms avg - 99 tokens - providers mock:1,openai_compatible:2 - failed gates model_latency_recorded_for_turns - model_latency_recorded_for_turns: fail - token_usage_aggregation_available: ok"
    );
    expect(summary).not.toContain("models.internal");
    expect(summary).not.toContain("secret-token");
  });

  it("formats asset production observability without raw storage evidence", () => {
    const summary = assetProductionObservabilitySummary({
      asset_count: 4,
      completed_asset_count: 3,
      failed_asset_count: 1,
      duration_recorded_asset_count: 2,
      size_recorded_asset_count: 3,
      failure_rate: 0.25,
      by_asset_type: {
        audio: { asset_count: 2, object_storage_path: "s3://private/audio.wav" },
        render: { asset_count: 1, storage_uri: "file:///private/render.mp4" }
      },
      by_language: {
        de: { asset_count: 1 },
        en: { asset_count: 3 }
      }
    });

    expect(summary).toBe(
      "4 assets - 3 completed - 1 failed - 2 duration records - 3 size records - 25% failure - types audio:2,render:1 - languages de:1,en:3"
    );
    expect(summary).not.toContain("s3://private");
    expect(summary).not.toContain("file:///private");
  });

  it("formats workflow duration observability without raw run identifiers", () => {
    const summary = workflowDurationObservabilitySummary({
      production_duration_record_count: 1,
      stage_duration_record_count: 3,
      production_duration_ms_sum: 12000,
      stage_duration_ms_sum: 12000,
      by_stage: {
        DRAFT: { duration_record_count: 1, run_id: "run-secret" },
        DISCUSSING: { duration_record_count: 2, episode_id: "episode-secret" }
      },
      by_language: {
        en: { duration_record_count: 1 },
        de: { duration_record_count: 1 }
      }
    });

    expect(summary).toBe(
      "1 run records - 3 stage records - 12000 ms runs - 12000 ms stages - stages DISCUSSING:2,DRAFT:1 - languages de:1,en:1"
    );
    expect(summary).not.toContain("run-secret");
    expect(summary).not.toContain("episode-secret");
  });

  it("formats queue wait observability without raw timestamp sources", () => {
    const summary = queueWaitObservabilitySummary({
      pending_wait_record_count: 1,
      completed_wait_record_count: 2,
      pending_wait_ms_sum: 3000,
      completed_wait_ms_sum: 21000,
      by_queue: {
        audio: { pending_wait_record_count: 1, completed_wait_record_count: 1 },
        publish_job: {
          pending_wait_record_count: 0,
          completed_wait_record_count: 1,
          job_id: "publish-secret"
        }
      },
      by_language: {
        en: { pending_wait_record_count: 1, completed_wait_record_count: 1 },
        de: { pending_wait_record_count: 0, completed_wait_record_count: 1 }
      },
      timestamp_sources: {
        asset_submitted_at: ["generation_metadata.submitted_at"]
      }
    });

    expect(summary).toBe(
      "1 pending records - 2 completed records - 3000 ms pending - 21000 ms completed - pending queues audio:1 - completed queues audio:1,publish_job:1 - pending languages en:1 - completed languages de:1,en:1"
    );
    expect(summary).not.toContain("publish-secret");
    expect(summary).not.toContain("generation_metadata");
  });
});
