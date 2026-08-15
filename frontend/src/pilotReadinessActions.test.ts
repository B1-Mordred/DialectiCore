import { describe, expect, it } from "vitest";
import { pilotReadinessAction } from "./pilotReadinessActions";

describe("pilot readiness actions", () => {
  it("routes character setup blockers to the Characters page", () => {
    expect(
      pilotReadinessAction({
        blockers: ["one or more selected characters are missing portrait references"],
      })
    ).toMatchObject({
      key: "characters",
      label: "Open Characters",
    });
  });

  it("uses failed readiness gates when blocker text is not present", () => {
    expect(
      pilotReadinessAction({
        stages: [
          {
            category: "visuals",
            details: {
              failed_readiness_checks: ["selected_visual_profiles_have_full_body_references"],
            },
          },
        ],
      })
    ).toMatchObject({
      key: "characters",
      reason: "selected_visual_profiles_have_full_body_references",
    });
  });

  it("does not route provider infrastructure blockers to character setup", () => {
    expect(
      pilotReadinessAction({
        blockers: ["one or more selected native ComfyUI endpoints block prompt admission"],
      })
    ).toBeNull();
  });
});
