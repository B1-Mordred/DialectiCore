export type DeliveryArtifactAsset = {
  id: string;
  asset_type: string;
  source_entity_type?: string | null;
  source_entity_id?: string | null;
  storage_uri?: string | null;
  mime_type?: string | null;
  duration_ms?: number | null;
  width?: number | null;
  height?: number | null;
  checksum?: string | null;
  generation_metadata?: Record<string, unknown>;
  status: string;
};

export type DeliveryArtifactEpisode = {
  assets: DeliveryArtifactAsset[];
  canonical_transcript_version_id?: string | null;
};

export type DeliveryArtifactId =
  | "preview_render"
  | "final_render"
  | "thumbnail"
  | "export_package"
  | "production_manifest";

export type DeliveryArtifactRow = {
  id: DeliveryArtifactId;
  label: string;
  description: string;
  asset: DeliveryArtifactAsset | null;
  statusLabel: string;
  detail: string;
  downloadable: boolean;
};

const DELIVERY_ARTIFACT_META: Record<
  DeliveryArtifactId,
  Pick<DeliveryArtifactRow, "label" | "description">
> = {
  preview_render: {
    label: "Preview Render",
    description: "Operator review render before final production."
  },
  final_render: {
    label: "Final Render",
    description: "Approved production video used for delivery."
  },
  thumbnail: {
    label: "Thumbnail",
    description: "Render-linked upload thumbnail."
  },
  export_package: {
    label: "YouTube Package",
    description: "ZIP bundle with video, thumbnail, captions, and metadata."
  },
  production_manifest: {
    label: "Production Manifest",
    description: "Audit manifest tying package, render, QC, and publish evidence together."
  }
};

export function deliveryArtifactSummary(episode: DeliveryArtifactEpisode | null): DeliveryArtifactRow[] {
  const finalRender = latestActiveAsset(episode, "render", (asset) =>
    isCurrentTalkshowRenderAsset(episode, asset) &&
    asset.generation_metadata?.render_type === "final"
  );
  const previewRender = latestActiveAsset(episode, "render", (asset) =>
    isCurrentTalkshowRenderAsset(episode, asset) &&
    asset.generation_metadata?.render_type === "preview"
  );
  const thumbnail =
    linkedLatestActiveAsset(episode, "thumbnail", "render_asset", finalRender?.id) ??
    (episode?.canonical_transcript_version_id
      ? null
      : latestActiveAsset(episode, "thumbnail"));
  const exportPackage =
    linkedLatestActiveAsset(episode, "export_package", "render_asset", finalRender?.id) ??
    (episode?.canonical_transcript_version_id
      ? null
      : latestActiveAsset(episode, "export_package"));
  const productionManifest =
    linkedLatestActiveAsset(episode, "production_manifest", "export_package", exportPackage?.id) ??
    (episode?.canonical_transcript_version_id
      ? null
      : latestActiveAsset(episode, "production_manifest"));

  return [
    deliveryArtifactRow("preview_render", previewRender),
    deliveryArtifactRow("final_render", finalRender),
    deliveryArtifactRow("thumbnail", thumbnail),
    deliveryArtifactRow("export_package", exportPackage),
    deliveryArtifactRow("production_manifest", productionManifest)
  ];
}

function isCurrentTalkshowRenderAsset(
  episode: DeliveryArtifactEpisode | null,
  asset: DeliveryArtifactAsset
): boolean {
  const canonicalTranscriptId = episode?.canonical_transcript_version_id;
  if (!canonicalTranscriptId) {
    return true;
  }
  if (asset.source_entity_type !== "timeline_asset" || !asset.source_entity_id) {
    return false;
  }
  const timeline = latestTalkshowTimeline(episode);
  return Boolean(
    timeline &&
      asset.source_entity_id === timeline.id
  );
}

function latestTalkshowTimeline(
  episode: DeliveryArtifactEpisode | null
): DeliveryArtifactAsset | null {
  const canonicalTranscriptId = episode?.canonical_transcript_version_id;
  if (!canonicalTranscriptId) {
    return null;
  }
  const timelines =
    episode?.assets.filter(
      (asset) =>
        asset.asset_type === "timeline" &&
        asset.status === "completed" &&
        asset.source_entity_type === "transcript_version" &&
        asset.source_entity_id === canonicalTranscriptId
    ) ?? [];
  return timelines.length > 0 ? timelines[timelines.length - 1] : null;
}

export function deliveryArtifactRow(
  id: DeliveryArtifactId,
  asset: DeliveryArtifactAsset | null
): DeliveryArtifactRow {
  const meta = DELIVERY_ARTIFACT_META[id];
  return {
    id,
    label: meta.label,
    description: meta.description,
    asset,
    statusLabel: deliveryArtifactStatus(asset),
    detail: deliveryArtifactDetail(asset),
    downloadable: isDownloadableDeliveryAsset(asset)
  };
}

export function isDownloadableDeliveryAsset(asset: DeliveryArtifactAsset | null): boolean {
  return Boolean(asset?.id && asset.status === "completed" && asset.storage_uri);
}

function latestActiveAsset(
  episode: DeliveryArtifactEpisode | null,
  assetType: string,
  predicate: (asset: DeliveryArtifactAsset) => boolean = () => true
): DeliveryArtifactAsset | null {
  const assets =
    episode?.assets.filter(
      (asset) => asset.asset_type === assetType && asset.status !== "replaced" && predicate(asset)
    ) ?? [];
  return assets.length > 0 ? assets[assets.length - 1] : null;
}

function linkedLatestActiveAsset(
  episode: DeliveryArtifactEpisode | null,
  assetType: string,
  sourceEntityType: string,
  sourceEntityId: string | null | undefined
): DeliveryArtifactAsset | null {
  if (!sourceEntityId) {
    return null;
  }
  return latestActiveAsset(
    episode,
    assetType,
    (asset) => asset.source_entity_type === sourceEntityType && asset.source_entity_id === sourceEntityId
  );
}

function deliveryArtifactStatus(asset: DeliveryArtifactAsset | null): string {
  if (!asset) {
    return "missing";
  }
  if (asset.status === "completed" && !asset.storage_uri) {
    return "completed, no file";
  }
  return asset.status;
}

function deliveryArtifactDetail(asset: DeliveryArtifactAsset | null): string {
  if (!asset) {
    return "Not generated yet";
  }
  const details = [
    shortId(asset.id),
    mediaShape(asset),
    durationLabel(asset.duration_ms),
    mimeLabel(asset.mime_type),
    checksumLabel(asset.checksum)
  ].filter((detail): detail is string => Boolean(detail));
  return details.length > 0 ? details.join(" · ") : shortId(asset.id);
}

function mediaShape(asset: DeliveryArtifactAsset): string | null {
  if (asset.width && asset.height) {
    return `${asset.width}x${asset.height}`;
  }
  return null;
}

function durationLabel(durationMs: number | null | undefined): string | null {
  if (!durationMs) {
    return null;
  }
  return `${Math.round(durationMs / 1000)}s`;
}

function mimeLabel(mimeType: string | null | undefined): string | null {
  return mimeType || null;
}

function checksumLabel(checksum: string | null | undefined): string | null {
  if (!checksum) {
    return null;
  }
  const compactChecksum = shortId(checksum);
  return checksum.startsWith("sha") ? compactChecksum : `sha ${compactChecksum}`;
}

function shortId(value: string): string {
  return value.length > 12 ? value.slice(0, 8) : value;
}
