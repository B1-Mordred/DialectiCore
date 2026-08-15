import { describe, expect, it } from "vitest";
import { productionReportOperatorActionSummary } from "./productionReportOperatorActions";

describe("production report operator actions", () => {
  it("surfaces approval navigation from completion failed checks", () => {
    expect(
      productionReportOperatorActionSummary({
        completion: {
          failed_checks: ["final_render_approval_missing"],
        },
      })
    ).toMatchObject({
      showApprovals: true,
      showPublishDryRun: false,
      showDeliverableDownloads: false,
    });
  });

  it("surfaces approval navigation from workflow handoff review actions", () => {
    expect(
      productionReportOperatorActionSummary({
        operator_next_actions: [
          {
            scope: "workflow",
            action: "review_final_render",
            status: "review_ready",
          },
        ],
      })
    ).toMatchObject({
      actions: ["review_final_render"],
      showApprovals: true,
      showTranscriptReview: false,
      showPublishDryRun: false,
      showDeliverableDownloads: false,
    });
  });

  it("surfaces transcript review navigation when the workflow waits for transcript approval", () => {
    expect(
      productionReportOperatorActionSummary({
        operator_next_actions: [
          {
            scope: "workflow",
            action: "approve_broadcast_transcript",
            status: "blocked",
          },
        ],
      })
    ).toMatchObject({
      actions: ["approve_broadcast_transcript"],
      showApprovals: true,
      showTranscriptReview: true,
      showPublishDryRun: false,
      showDeliverableDownloads: false,
    });
  });

  it("surfaces delivery downloads after a passing production report", () => {
    expect(
      productionReportOperatorActionSummary({
        operator_next_action: "inspect_export_package_and_publish_evidence",
        deliverables: {
          export_package: {
            downloadable: true,
            download_url: "/api/v1/episodes/example/assets/package/download",
          },
        },
      })
    ).toMatchObject({
      actions: ["inspect_export_package_and_publish_evidence"],
      showDeliverableDownloads: true,
    });
  });

  it("surfaces dry-run publishing from parallel operator actions", () => {
    expect(
      productionReportOperatorActionSummary({
        operator_next_actions: [
          {
            scope: "publishing",
            action: "run_dry_run_publish_for_real_life_test",
          },
        ],
      })
    ).toMatchObject({
      showPublishDryRun: true,
    });
  });
});
