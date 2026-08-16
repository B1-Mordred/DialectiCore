export type TrimmableTimelineClip = {
  start_ms: number;
  end_ms: number;
  duration_ms?: number;
  source_in_ms?: number;
  source_out_ms?: number;
  [key: string]: unknown;
};

const MINIMUM_CLIP_DURATION_MS = 100;

export function canonicalCameraView(value: unknown): string {
  const view = String(value ?? "speaker_medium");
  return {
    speaker_centered: "speaker_medium",
    speaker_close: "speaker_close_up",
  }[view] ?? view;
}

function clamp(value: number, minimum: number, maximum: number): number {
  return Math.min(Math.max(value, minimum), Math.max(minimum, maximum));
}

export function positiveDurationMs(...candidates: unknown[]): number | null {
  for (const candidate of candidates) {
    const value = Number(candidate);
    if (Number.isFinite(value) && value > 0) {
      return Math.round(value);
    }
  }
  return null;
}

export function trimTimelineClip(
  clip: TrimmableTimelineClip,
  boundary: "start" | "end",
  requestedProgrammeMs: number,
  options: {
    programmeDurationMs: number;
    sourceAware?: boolean;
    sourceDurationMs?: number | null;
  },
): TrimmableTimelineClip {
  if (!Number.isFinite(requestedProgrammeMs)) return clip;
  const programmeDurationMs = Math.max(0, Number(options.programmeDurationMs));
  const startMs = Number(clip.start_ms);
  const endMs = Number(clip.end_ms);
  const sourceInMs = Number(clip.source_in_ms ?? 0);
  const sourceOutMs = Number(clip.source_out_ms ?? sourceInMs + endMs - startMs);
  const sourceDurationMs = positiveDurationMs(options.sourceDurationMs);

  if (boundary === "start") {
    const earliestStartMs = options.sourceAware
      ? Math.max(0, startMs - sourceInMs)
      : 0;
    const nextStartMs = Math.round(
      clamp(requestedProgrammeMs, earliestStartMs, endMs - MINIMUM_CLIP_DURATION_MS),
    );
    const sourceDeltaMs = nextStartMs - startMs;
    return {
      ...clip,
      start_ms: nextStartMs,
      duration_ms: endMs - nextStartMs,
      ...(options.sourceAware
        ? { source_in_ms: Math.max(0, sourceInMs + sourceDeltaMs) }
        : {}),
    };
  }

  const latestEndMs = options.sourceAware && sourceDurationMs
    ? Math.min(programmeDurationMs, endMs + sourceDurationMs - sourceOutMs)
    : programmeDurationMs;
  const nextEndMs = Math.round(
    clamp(requestedProgrammeMs, startMs + MINIMUM_CLIP_DURATION_MS, latestEndMs),
  );
  const sourceDeltaMs = nextEndMs - endMs;
  return {
    ...clip,
    end_ms: nextEndMs,
    duration_ms: nextEndMs - startMs,
    ...(options.sourceAware
      ? { source_out_ms: Math.max(sourceInMs + MINIMUM_CLIP_DURATION_MS, sourceOutMs + sourceDeltaMs) }
      : {}),
  };
}

export function setSourceBoundaryPreservingDuration(
  clip: TrimmableTimelineClip,
  boundary: "in" | "out",
  requestedSourceMs: number,
  sourceDurationMs?: number | null,
): TrimmableTimelineClip {
  if (!Number.isFinite(requestedSourceMs)) return clip;
  const clipDurationMs = Math.max(
    MINIMUM_CLIP_DURATION_MS,
    Number(clip.end_ms) - Number(clip.start_ms),
  );
  const knownSourceDurationMs = positiveDurationMs(sourceDurationMs);
  if (boundary === "in") {
    const latestInMs = knownSourceDurationMs
      ? Math.max(0, knownSourceDurationMs - clipDurationMs)
      : Number.MAX_SAFE_INTEGER;
    const sourceInMs = Math.round(clamp(requestedSourceMs, 0, latestInMs));
    return {
      ...clip,
      source_in_ms: sourceInMs,
      source_out_ms: sourceInMs + clipDurationMs,
    };
  }
  const latestOutMs = knownSourceDurationMs ?? Number.MAX_SAFE_INTEGER;
  const sourceOutMs = Math.round(
    clamp(requestedSourceMs, clipDurationMs, latestOutMs),
  );
  return {
    ...clip,
    source_in_ms: sourceOutMs - clipDurationMs,
    source_out_ms: sourceOutMs,
  };
}

export function zoomForClip(
  programmeDurationMs: number,
  clipDurationMs: number,
  targetPixels = 180,
  baseWidthPixels = 560,
): number {
  if (programmeDurationMs <= 0 || clipDurationMs <= 0) return 1;
  const required = (targetPixels * programmeDurationMs) / (baseWidthPixels * clipDurationMs);
  return [1, 2, 4, 8, 16, 32].find((zoom) => zoom >= required) ?? 32;
}
