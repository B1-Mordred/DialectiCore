import { describe, expect, it } from "vitest";
import { activeManagedMediaJobCount } from "./productionActivity";

describe("production activity", () => {
  it("counts queued B1 seated-character work as active media", () => {
    expect(
      activeManagedMediaJobCount([
        {
          status: "submitted",
          generation_metadata: {
            visual_role: "studio_seated_character",
            adapter: "b1_managed_media",
            remote_job_id: "job-seated-1",
          },
        },
        {
          status: "running",
          generation_metadata: {
            visual_role: "studio_seated_character",
            managed_media_api_base: "https://api.example.test",
          },
        },
      ]),
    ).toBe(2);
  });

  it("does not mistake locally planned or completed assets for active remote jobs", () => {
    expect(
      activeManagedMediaJobCount([
        { status: "planned", generation_metadata: { remote_job_id: "job-planned" } },
        { status: "completed", generation_metadata: { remote_job_id: "job-complete" } },
        { status: "submitted", generation_metadata: {} },
      ]),
    ).toBe(0);
  });
});
