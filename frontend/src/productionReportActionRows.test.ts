import { describe, expect, it } from "vitest";
import { productionReportActionRows } from "./productionReportActionRows";

describe("production report action rows", () => {
  it("formats parallel scoped operator actions for scan-friendly UI rows", () => {
    expect(
      productionReportActionRows({
        provider_repair_handoff: {
          status: "present",
          path: "/home/mordred/media-requirements.md",
          section_count: 4,
          latest_sections: ["DialectiCore Production Provider Handoff Added"],
          has_voicebox_requirements: true,
          has_managed_media_requirements: true
        },
        operator_next_actions: [
          {
            scope: "speech",
            action: "fix_voicebox_generation_then_retry_audio_assets",
            status: "fail"
          },
          {
            scope: "managed_media",
            action: "fix_b1_managed_media_runner_then_rerun_smoke",
            status: "runner_failed"
          },
          {
            scope: "native_visual",
            action: "retry_fallback_visuals_as_native_after_b1_fix",
            status: "fallback_visuals_present",
            asset_count: 13
          }
        ]
      })
    ).toMatchObject([
      {
        scope: "Speech",
        title: "Fix Voicebox, Then Retry Speech",
        detail:
          "Speech generation and Voicebox output · Repair handoff present at /home/mordred/media-requirements.md, 4 sections, voicebox noted, B1 media noted, latest DialectiCore Production Provider Handoff Added",
        priority: "fail"
      },
      {
        scope: "B1 Media",
        title: "Fix B1 Runner, Then Rerun Smoke",
        detail:
          "B1 managed media execution · Repair handoff present at /home/mordred/media-requirements.md, 4 sections, voicebox noted, B1 media noted, latest DialectiCore Production Provider Handoff Added",
        priority: "fail"
      },
      {
        scope: "Native Visuals",
        title: "Retry Fallback Visuals As Native",
        detail:
          "Character animation and native visual handoff · Repair handoff present at /home/mordred/media-requirements.md, 4 sections, voicebox noted, B1 media noted, latest DialectiCore Production Provider Handoff Added · 13 assets",
        priority: "pass"
      }
    ]);
  });

  it("shows missing repair handoff on B1 provider repair actions", () => {
    expect(
      productionReportActionRows({
        provider_repair_handoff: {
          status: "missing",
          path: "/home/mordred/media-requirements.md",
          exists: false
        },
        operator_next_actions: [
          {
            scope: "managed_media",
            action: "fix_b1_managed_media_runner_then_retry_visual_assets",
            status: "fail"
          }
        ]
      })[0]
    ).toMatchObject({
      scope: "B1 Media",
      title: "Fix B1 Runner, Then Retry Visuals",
      detail:
        "B1 managed media execution · Repair handoff missing at /home/mordred/media-requirements.md",
      priority: "fail"
    });
  });

  it("falls back to the primary operator action when scoped actions are absent", () => {
    expect(
      productionReportActionRows({
        operator_next_action: "run_dry_run_publish_for_real_life_test"
      })
    ).toEqual([
      {
        key: "next:run_dry_run_publish_for_real_life_test:0",
        scope: "Next Action",
        title: "Run Dry-Run Publish",
        status: "pending",
        detail: "Inspect the production report evidence.",
        priority: "warning"
      }
    ]);
  });

  it("formats workflow handoff actions with review and blocker context", () => {
    expect(
      productionReportActionRows({
        operator_next_actions: [
          {
            scope: "workflow",
            action: "review_preview_render",
            status: "review_ready",
            stop_reason: "pending_approval",
            pending_approval_stages: ["preview_render_review"],
            blocking_reasons: ["completed_audio_missing"]
          }
        ]
      })[0]
    ).toEqual({
      key: "workflow:review_preview_render:0",
      scope: "Workflow",
      title: "Review Preview Render",
      status: "review_ready",
      detail:
        "Run Until Review handoff · stopped at pending approval, review Preview Render, blockers completed audio missing",
      priority: "warning"
    });
  });

  it("formats live provider preflight actions with failed model and voice counts", () => {
    expect(
      productionReportActionRows({
        operator_next_actions: [
          {
            scope: "live_provider_preflight",
            action: "fix_voicebox_generation_then_rerun_live_preflight",
            status: "fail",
            blocking_sections: ["voicebox"],
            model_failed_count: 0,
            voicebox_failed_count: 6
          }
        ]
      })[0]
    ).toEqual({
      key: "live_provider_preflight:fix_voicebox_generation_then_rerun_live_preflight:0",
      scope: "Live Providers",
      title: "Fix Voicebox, Then Rerun Preflight",
      status: "fail",
      detail:
        "Frontier cast model and voice preflight · blocked voicebox, 0 model failed, 6 voice failed",
      priority: "fail"
    });
  });

  it("labels mixed live provider preflight failures as provider-wide repair", () => {
    expect(
      productionReportActionRows({
        operator_next_actions: [
          {
            scope: "live_provider_preflight",
            action: "fix_live_provider_failures_then_rerun_preflight",
            status: "fail",
            blocking_sections: ["openrouter", "voicebox"],
            model_failed_count: 1,
            voicebox_failed_count: 6
          }
        ]
      })[0]
    ).toMatchObject({
      scope: "Live Providers",
      title: "Fix Live Providers, Then Rerun Preflight",
      detail:
        "Frontier cast model and voice preflight · blocked openrouter, voicebox, 1 model failed, 6 voice failed"
    });
  });

  it("returns no rows when no action evidence is available", () => {
    expect(productionReportActionRows({})).toEqual([]);
    expect(productionReportActionRows(null)).toEqual([]);
  });
});
