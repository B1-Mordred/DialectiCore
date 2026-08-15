import { describe, expect, it } from "vitest";
import { actionErrorMessage, apiErrorDetail, apiRequestErrorMessage } from "./apiErrors";

describe("API error formatting", () => {
  it("uses FastAPI string detail in request failures", () => {
    expect(
      apiRequestErrorMessage(
        422,
        JSON.stringify({ detail: "voicebox endpoint is not a B1 stream adapter" })
      )
    ).toBe("Request failed 422: voicebox endpoint is not a B1 stream adapter");
  });

  it("compacts FastAPI validation arrays", () => {
    expect(
      apiErrorDetail(
        JSON.stringify({
          detail: [
            { loc: ["body", "id"], msg: "Field required" },
            { loc: ["body", "base_url"], msg: "Input should be a valid URL" }
          ]
        })
      )
    ).toBe("body.id: Field required; body.base_url: Input should be a valid URL");
  });

  it("falls back to text and generic error objects", () => {
    expect(apiRequestErrorMessage(500, "  remote adapter unavailable  ")).toBe(
      "Request failed 500: remote adapter unavailable"
    );
    expect(actionErrorMessage("network failed")).toBe("network failed");
  });
});
