import { describe, expect, it } from "vitest";
import {
  canonicalCameraView,
  positiveDurationMs,
  setSourceBoundaryPreservingDuration,
  trimTimelineClip,
  zoomForClip,
} from "./timelineEditing";

describe("timeline editing", () => {
  const clip = {
    start_ms: 10_000,
    end_ms: 15_000,
    duration_ms: 5_000,
    source_in_ms: 2_000,
    source_out_ms: 7_000,
  };

  it("shows legacy camera values as their supported native panel view", () => {
    expect(canonicalCameraView("speaker_centered")).toBe("speaker_medium");
    expect(canonicalCameraView("speaker_close")).toBe("speaker_close_up");
    expect(canonicalCameraView("establishing_wide")).toBe("establishing_wide");
  });

  it("trims a B-roll start and advances the source clock by the same amount", () => {
    expect(
      trimTimelineClip(clip, "start", 12_000, {
        programmeDurationMs: 60_000,
        sourceAware: true,
        sourceDurationMs: 20_000,
      }),
    ).toMatchObject({
      start_ms: 12_000,
      end_ms: 15_000,
      duration_ms: 3_000,
      source_in_ms: 4_000,
      source_out_ms: 7_000,
    });
  });

  it("does not extend a B-roll edge beyond available source media", () => {
    expect(
      trimTimelineClip(clip, "end", 30_000, {
        programmeDurationMs: 60_000,
        sourceAware: true,
        sourceDurationMs: 8_000,
      }),
    ).toMatchObject({ end_ms: 16_000, source_out_ms: 8_000, duration_ms: 6_000 });
  });

  it("preserves clip length when a preview frame becomes source in or out", () => {
    expect(setSourceBoundaryPreservingDuration(clip, "in", 6_000, 20_000)).toMatchObject({
      source_in_ms: 6_000,
      source_out_ms: 11_000,
    });
    expect(setSourceBoundaryPreservingDuration(clip, "out", 12_000, 20_000)).toMatchObject({
      source_in_ms: 7_000,
      source_out_ms: 12_000,
    });
  });

  it("ignores null and zero duration metadata and chooses a useful focus zoom", () => {
    expect(positiveDurationMs(null, 0, 122_365.9)).toBe(122_366);
    expect(zoomForClip(364_000, 5_000)).toBe(32);
    expect(zoomForClip(60_000, 15_000)).toBe(2);
  });

  it("ignores temporarily empty precision inputs instead of corrupting timing", () => {
    expect(
      trimTimelineClip(clip, "start", Number.NaN, { programmeDurationMs: 60_000 }),
    ).toBe(clip);
    expect(setSourceBoundaryPreservingDuration(clip, "in", Number.NaN, 20_000)).toBe(
      clip,
    );
  });
});
