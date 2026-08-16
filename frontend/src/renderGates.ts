export type RenderGateAsset = {
  id: string;
  asset_type: string;
  status: string;
  source_entity_type?: string | null;
  source_entity_id?: string | null;
  generation_metadata?: Record<string, unknown>;
};

export type RenderGateApproval = {
  stage: string;
  target_type?: string | null;
  target_id?: string | null;
  decision: string;
};

export type RenderGateQualityResult = {
  check_type: string;
  target_id: string;
  status: string;
  severity?: string;
};

export type RenderGateEpisode = {
  status: string;
  assets: RenderGateAsset[];
  approvals: RenderGateApproval[];
  quality_results?: RenderGateQualityResult[];
};

export function canRenderPreview(episode: RenderGateEpisode | null): boolean {
  if (!episode || episode.status !== "READY") {
    return false;
  }
  const timeline = latestCompletedTimelineAsset(episode);
  if (!timeline) {
    return false;
  }
  if (!timelineIntegrityPasses(episode, timeline)) {
    return false;
  }
  return (
    activeOrCompletedRenderForTimeline(
      episode,
      timeline,
      "preview",
      "preview-low-bitrate",
    ) === null
  );
}

function timelineIntegrityPasses(
  episode: RenderGateEpisode,
  timeline: RenderGateAsset,
): boolean {
  const timelineJson = timeline.generation_metadata?.timeline_json;
  const media =
    timelineJson && typeof timelineJson === "object" && !Array.isArray(timelineJson)
      ? (timelineJson as Record<string, unknown>).media
      : null;
  const requiresIntegrityGate =
    media &&
    typeof media === "object" &&
    !Array.isArray(media) &&
    [
      "studio_camera_cuts.v1",
      "studio_camera_cuts.v2",
      "seated_studio_panel.v1",
    ].includes(
      String((media as Record<string, unknown>).composition_policy),
    );
  if (!requiresIntegrityGate) {
    return true;
  }
  const result = [...(episode.quality_results ?? [])]
    .reverse()
    .find(
      (quality) =>
        quality.check_type === "timeline_integrity" && quality.target_id === timeline.id,
    );
  return Boolean(
    result && result.status !== "fail" && result.severity !== "fail",
  );
}

export function canRenderFinal(episode: RenderGateEpisode | null): boolean {
  if (!episode || episode.status !== "READY") {
    return false;
  }
  const timeline = latestCompletedTimelineAsset(episode);
  if (!timeline) {
    return false;
  }
  if (!timelineIntegrityPasses(episode, timeline)) {
    return false;
  }
  const previewRender = latestRenderForTimeline(episode, timeline, "preview");
  if (!previewRender || !previewRenderApproved(episode, previewRender)) {
    return false;
  }
  return (
    activeOrCompletedRenderForTimeline(
      episode,
      timeline,
      "final",
      "youtube-1080p",
    ) ===
    null
  );
}

function latestCompletedTimelineAsset(
  episode: RenderGateEpisode,
): RenderGateAsset | null {
  const timelines = episode.assets.filter(
    (asset) => asset.asset_type === "timeline" && asset.status === "completed",
  );
  return timelines.length > 0 ? timelines[timelines.length - 1] : null;
}

function latestRenderForTimeline(
  episode: RenderGateEpisode,
  timeline: RenderGateAsset,
  renderType: "preview" | "final",
  presetId?: string,
): RenderGateAsset | null {
  const renders = episode.assets.filter(
    (asset) =>
      asset.asset_type === "render" &&
      asset.status === "completed" &&
      asset.source_entity_type === "timeline_asset" &&
      asset.source_entity_id === timeline.id &&
      asset.generation_metadata?.render_type === renderType &&
      asset.generation_metadata?.review_scope === "full_timeline" &&
      !renderIntegrityFailed(episode, asset) &&
      (presetId === undefined ||
        asset.generation_metadata?.preset_id === presetId),
  );
  return renders.length > 0 ? renders[renders.length - 1] : null;
}

function activeOrCompletedRenderForTimeline(
  episode: RenderGateEpisode,
  timeline: RenderGateAsset,
  renderType: "preview" | "final",
  presetId: string,
): RenderGateAsset | null {
  const renders = episode.assets.filter(
    (asset) =>
      asset.asset_type === "render" &&
      ["submitted", "running", "completed"].includes(asset.status) &&
      asset.source_entity_type === "timeline_asset" &&
      asset.source_entity_id === timeline.id &&
      asset.generation_metadata?.render_type === renderType &&
      asset.generation_metadata?.review_scope === "full_timeline" &&
      (asset.status !== "completed" || !renderIntegrityFailed(episode, asset)) &&
      asset.generation_metadata?.preset_id === presetId,
  );
  return renders.length > 0 ? renders[renders.length - 1] : null;
}

function previewRenderApproved(
  episode: RenderGateEpisode,
  renderAsset: RenderGateAsset,
): boolean {
  if (renderIntegrityFailed(episode, renderAsset)) {
    return false;
  }
  if (renderAsset.generation_metadata?.approval_status === "approved") {
    return true;
  }
  return episode.approvals.some(
    (approval) =>
      approval.stage === "preview_render_review" &&
      approval.target_type === "render_asset" &&
      approval.target_id === renderAsset.id &&
      approval.decision === "approved",
  );
}

function renderIntegrityFailed(
  episode: RenderGateEpisode,
  asset: RenderGateAsset,
): boolean {
  const renderType = asset.generation_metadata?.render_type;
  if (typeof renderType !== "string") {
    return false;
  }
  const result = [...(episode.quality_results ?? [])]
    .reverse()
    .find(
      (quality) =>
        quality.target_id === asset.id &&
        quality.check_type === `render_${renderType}_integrity`,
    );
  return Boolean(result && (result.status === "fail" || result.severity === "fail"));
}
