import { describe, expect, it } from "vitest";
import {
  characterReadinessBadges,
  characterReadinessSummary,
  hasVisualReference,
} from "./characterReadiness";

describe("character readiness helpers", () => {
  it("marks model, voice, portrait, and full-body setup ready", () => {
    const badges = characterReadinessBadges(
      {
        model_endpoint_id: "openrouter",
        model_id: "openai/gpt-4.1-mini",
        voice_profile_id: "voice-chatgpt",
        visual_profile_id: "visual-chatgpt",
      },
      {
        reference_images: [
          { reference_type: "portrait", uri: "object://portrait.png" },
          { reference_type: "full_body", uri: "object://full-body.png" },
        ],
      }
    );

    expect(badges.map((badge) => [badge.key, badge.ready])).toEqual([
      ["model", true],
      ["voice", true],
      ["portrait", true],
      ["full_body", true],
    ]);
  });

  it("summarizes missing production setup without exposing IDs", () => {
    expect(
      characterReadinessSummary(
        {
          model_endpoint_id: "",
          model_id: "model-a",
          voice_profile_id: null,
          visual_profile_id: "visual-a",
        },
        {
          reference_images: [{ reference_type: "portrait", uri: "object://portrait.png" }],
        }
      )
    ).toBe("missing model endpoint or model id; missing voice profile; missing full-body reference");
  });

  it("treats the legacy portrait URI as a portrait reference", () => {
    expect(
      hasVisualReference(
        {
          reference_image_uri: "object://legacy-portrait.png",
          reference_images: [],
        },
        "portrait"
      )
    ).toBe(true);
  });
});
