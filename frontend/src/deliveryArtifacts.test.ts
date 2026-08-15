import { describe, expect, it } from "vitest";
import { deliveryArtifactSummary, isDownloadableDeliveryAsset } from "./deliveryArtifacts";

describe("delivery artifact summaries", () => {
  it("excludes primer renders from a canonical talk-show delivery path", () => {
    const rows = deliveryArtifactSummary({
      canonical_transcript_version_id: "transcript-current",
      assets: [
        {
          id: "primer-timeline",
          asset_type: "timeline",
          source_entity_type: "primer_production",
          source_entity_id: "primer-1",
          status: "completed"
        },
        {
          id: "primer-preview",
          asset_type: "render",
          source_entity_type: "timeline_asset",
          source_entity_id: "primer-timeline",
          generation_metadata: { render_type: "preview" },
          status: "completed",
          storage_uri: "s3://primer-preview"
        },
        {
          id: "talkshow-timeline",
          asset_type: "timeline",
          source_entity_type: "transcript_version",
          source_entity_id: "transcript-current",
          status: "completed"
        },
        {
          id: "talkshow-preview",
          asset_type: "render",
          source_entity_type: "timeline_asset",
          source_entity_id: "talkshow-timeline",
          generation_metadata: { render_type: "preview" },
          status: "completed",
          storage_uri: "s3://talkshow-preview"
        }
      ]
    });

    expect(rows.find((row) => row.id === "preview_render")?.asset?.id).toBe(
      "talkshow-preview"
    );
    expect(rows.find((row) => row.id === "final_render")?.asset).toBeNull();
  });

  it("does not let a rejected preview from a superseded timeline block delivery", () => {
    const rows = deliveryArtifactSummary({
      canonical_transcript_version_id: "transcript-current",
      assets: [
        {
          id: "timeline-old",
          asset_type: "timeline",
          source_entity_type: "transcript_version",
          source_entity_id: "transcript-current",
          status: "completed"
        },
        {
          id: "preview-old",
          asset_type: "render",
          source_entity_type: "timeline_asset",
          source_entity_id: "timeline-old",
          generation_metadata: { render_type: "preview" },
          status: "rejected",
          storage_uri: "s3://preview-old"
        },
        {
          id: "timeline-current",
          asset_type: "timeline",
          source_entity_type: "transcript_version",
          source_entity_id: "transcript-current",
          status: "completed"
        }
      ]
    });

    expect(rows.find((row) => row.id === "preview_render")?.asset).toBeNull();
  });

  it("links thumbnail, package, and manifest to the latest final render path", () => {
    const rows = deliveryArtifactSummary({
      assets: [
        {
          id: "preview-1",
          asset_type: "render",
          status: "completed",
          storage_uri: "s3://preview",
          generation_metadata: { render_type: "preview" }
        },
        {
          id: "final-old",
          asset_type: "render",
          status: "completed",
          storage_uri: "s3://old",
          generation_metadata: { render_type: "final" }
        },
        {
          id: "final-new",
          asset_type: "render",
          status: "completed",
          storage_uri: "s3://new",
          width: 1920,
          height: 1080,
          duration_ms: 125000,
          checksum: "abcdef1234567890",
          generation_metadata: { render_type: "final" }
        },
        {
          id: "package-old",
          asset_type: "export_package",
          source_entity_type: "render_asset",
          source_entity_id: "final-old",
          status: "completed",
          storage_uri: "s3://package-old"
        },
        {
          id: "thumb-new",
          asset_type: "thumbnail",
          source_entity_type: "render_asset",
          source_entity_id: "final-new",
          status: "completed",
          storage_uri: "s3://thumb-new"
        },
        {
          id: "package-new",
          asset_type: "export_package",
          source_entity_type: "render_asset",
          source_entity_id: "final-new",
          status: "completed",
          storage_uri: "s3://package-new"
        },
        {
          id: "manifest-new",
          asset_type: "production_manifest",
          source_entity_type: "export_package",
          source_entity_id: "package-new",
          status: "completed",
          storage_uri: "s3://manifest-new"
        }
      ]
    });

    expect(rows.map((row) => [row.id, row.asset?.id])).toEqual([
      ["preview_render", "preview-1"],
      ["final_render", "final-new"],
      ["thumbnail", "thumb-new"],
      ["export_package", "package-new"],
      ["production_manifest", "manifest-new"]
    ]);
    expect(rows.find((row) => row.id === "final_render")?.detail).toContain("1920x1080");
    expect(rows.every((row) => row.downloadable)).toBe(true);
  });

  it("shows pending and missing assets without exposing download links", () => {
    const rows = deliveryArtifactSummary({
      assets: [
        {
          id: "final-render",
          asset_type: "render",
          status: "completed",
          generation_metadata: { render_type: "final" }
        },
        {
          id: "package-pending",
          asset_type: "export_package",
          source_entity_type: "render_asset",
          source_entity_id: "final-render",
          status: "queued"
        }
      ]
    });

    expect(rows.find((row) => row.id === "final_render")).toMatchObject({
      statusLabel: "completed, no file",
      downloadable: false
    });
    expect(rows.find((row) => row.id === "thumbnail")).toMatchObject({
      asset: null,
      statusLabel: "missing",
      downloadable: false
    });
    expect(rows.find((row) => row.id === "export_package")).toMatchObject({
      statusLabel: "queued",
      downloadable: false
    });
  });

  it("requires completed storage-backed assets for generic downloads", () => {
    expect(
      isDownloadableDeliveryAsset({
        id: "asset-1",
        asset_type: "render",
        status: "completed",
        storage_uri: "file:///tmp/render.mp4"
      })
    ).toBe(true);
    expect(
      isDownloadableDeliveryAsset({
        id: "asset-2",
        asset_type: "render",
        status: "failed",
        storage_uri: "file:///tmp/render.mp4"
      })
    ).toBe(false);
  });
});
