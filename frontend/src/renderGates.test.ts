import { describe, expect, it } from "vitest";
import {
  canRenderFinal,
  canRenderPreview,
  type RenderGateAsset,
  type RenderGateEpisode,
} from "./renderGates";

const timeline: RenderGateAsset = {
  id: "timeline-current",
  asset_type: "timeline",
  status: "completed",
};

const directedTimeline: RenderGateAsset = {
  ...timeline,
  generation_metadata: {
    timeline_json: {
      media: { composition_policy: "studio_camera_cuts.v1" },
    },
  },
};

const directedTimelineV2: RenderGateAsset = {
  ...timeline,
  generation_metadata: {
    timeline_json: {
      media: { composition_policy: "studio_camera_cuts.v2" },
    },
  },
};

const seatedPanelTimeline: RenderGateAsset = {
  ...timeline,
  generation_metadata: {
    timeline_json: {
      media: { composition_policy: "seated_studio_panel.v1" },
    },
  },
};

function episode(
  overrides: Partial<RenderGateEpisode> = {},
): RenderGateEpisode {
  return {
    status: "READY",
    assets: [timeline],
    approvals: [],
    ...overrides,
  };
}

function render(
  id: string,
  renderType: "preview" | "final",
  options: {
    presetId?: string;
    timelineId?: string;
    approvalStatus?: string;
    status?: string;
    reviewScope?: "full_timeline" | "qualification_slice";
  } = {},
): RenderGateAsset {
  return {
    id,
    asset_type: "render",
    status: options.status ?? "completed",
    source_entity_type: "timeline_asset",
    source_entity_id: options.timelineId ?? timeline.id,
    generation_metadata: {
      render_type: renderType,
      review_scope: options.reviewScope ?? "full_timeline",
      preset_id:
        options.presetId ??
        (renderType === "preview" ? "preview-low-bitrate" : "youtube-1080p"),
      ...(options.approvalStatus
        ? { approval_status: options.approvalStatus }
        : {}),
    },
  };
}

describe("render gates", () => {
  it("allows the first preview render for a completed timeline", () => {
    expect(canRenderPreview(episode())).toBe(true);
  });

  it("does not let a preview from an older timeline block the current timeline", () => {
    expect(
      canRenderPreview(
        episode({
          assets: [
            timeline,
            render("preview-old", "preview", { timelineId: "timeline-old" }),
          ],
        }),
      ),
    ).toBe(true);
  });

  it("does not offer a duplicate preview while the current render is queued", () => {
    expect(
      canRenderPreview(
        episode({
          assets: [
            timeline,
            render("preview-queued", "preview", { status: "submitted" }),
          ],
        }),
      ),
    ).toBe(false);
  });

  it("does not let a qualification slice block or satisfy the full preview gate", () => {
    const qualification = render("preview-qualification", "preview", {
      reviewScope: "qualification_slice",
      approvalStatus: "approved",
    });

    expect(canRenderPreview(episode({ assets: [timeline, qualification] }))).toBe(true);
    expect(canRenderFinal(episode({ assets: [timeline, qualification] }))).toBe(false);
  });

  it("requires a passing integrity result for a studio-directed timeline", () => {
    expect(canRenderPreview(episode({ assets: [directedTimeline] }))).toBe(false);
    expect(
      canRenderPreview(
        episode({
          assets: [directedTimeline],
          quality_results: [
            {
              check_type: "timeline_integrity",
              target_id: directedTimeline.id,
              status: "pass",
              severity: "pass",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("requires the same integrity gate for virtual-camera timelines", () => {
    expect(canRenderPreview(episode({ assets: [directedTimelineV2] }))).toBe(false);
    expect(
      canRenderPreview(
        episode({
          assets: [directedTimelineV2],
          quality_results: [
            {
              check_type: "timeline_integrity",
              target_id: directedTimelineV2.id,
              status: "pass",
              severity: "pass",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("requires the integrity gate for seated studio panel timelines", () => {
    expect(canRenderPreview(episode({ assets: [seatedPanelTimeline] }))).toBe(false);
    expect(
      canRenderPreview(
        episode({
          assets: [seatedPanelTimeline],
          quality_results: [
            {
              check_type: "timeline_integrity",
              target_id: seatedPanelTimeline.id,
              status: "pass",
              severity: "pass",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("allows a non-blocking timeline integrity warning", () => {
    expect(
      canRenderPreview(
        episode({
          assets: [seatedPanelTimeline],
          quality_results: [
            {
              check_type: "timeline_integrity",
              target_id: seatedPanelTimeline.id,
              status: "warning",
              severity: "warning",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("requires the matching preview to be approved before final rendering", () => {
    const preview = render("preview-current", "preview");
    expect(canRenderFinal(episode({ assets: [timeline, preview] }))).toBe(
      false,
    );
    expect(
      canRenderFinal(
        episode({
          assets: [timeline, preview],
          approvals: [
            {
              stage: "preview_render_review",
              target_type: "render_asset",
              target_id: preview.id,
              decision: "approved",
            },
          ],
        }),
      ),
    ).toBe(true);
  });

  it("does not treat a failed-QC preview as current or reviewable", () => {
    const preview = render("preview-failed", "preview", {
      approvalStatus: "approved",
    });
    const failedEpisode = episode({
      assets: [timeline, preview],
      approvals: [
        {
          stage: "preview_render_review",
          target_type: "render_asset",
          target_id: preview.id,
          decision: "approved",
        },
      ],
      quality_results: [
        {
          check_type: "render_preview_integrity",
          target_id: preview.id,
          status: "fail",
          severity: "fail",
        },
      ],
    });

    expect(canRenderFinal(failedEpisode)).toBe(false);
    expect(canRenderPreview(failedEpisode)).toBe(true);
  });

  it("does not offer another final render for the same completed timeline and preset", () => {
    const preview = render("preview-current", "preview", {
      approvalStatus: "approved",
    });
    const finalRender = render("final-current", "final");
    expect(
      canRenderFinal(episode({ assets: [timeline, preview, finalRender] })),
    ).toBe(false);
  });
});
