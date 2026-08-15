import { describe, expect, it } from "vitest";
import { formatVoiceboxCapabilities, hasVoiceboxCaBootstrap } from "./voiceboxCapabilities";

describe("Voicebox capability summaries", () => {
  it("formats generic JSON Voicebox capabilities", () => {
    expect(
      formatVoiceboxCapabilities({
        formats: ["audio/wav", "audio/flac"],
        word_timestamps: true
      })
    ).toBe("audio/wav, audio/flac · json metadata · word timing");
  });

  it("formats B1 stream CA readiness without exposing URLs or hashes", () => {
    const summary = formatVoiceboxCapabilities({
      formats: ["audio/wav"],
      response_mode: "audio_stream",
      word_timestamps: false,
      ca_cert_bootstrap_url:
        "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt",
      ca_cert_sha256: "326339ee673c53ecca71b7006b177996dbec2c5166b94b2e3cfd56060612bb0c",
      tls_ca_cert_available: true,
      ca_cert_bootstrap: {
        stored: true,
        sha256_matches: true
      }
    });

    expect(summary).toBe("audio/wav · stream audio · no word timing · CA file ready/stored/SHA verified");
    expect(summary).not.toContain("voice.ai.b1.germering");
    expect(summary).not.toContain("326339ee");
  });

  it("formats B1 stream credential readiness without exposing references or errors", () => {
    const summary = formatVoiceboxCapabilities({
      formats: ["audio/wav"],
      response_mode: "audio_stream",
      credential_reference_configured: true,
      credential_reference_resolved: false,
      credential_reference: "env:B1_API_KEY",
      credential_reference_error: "credential reference is not available"
    });

    expect(summary).toBe("audio/wav · stream audio · no word timing · credential unavailable");
    expect(summary).not.toContain("B1_API_KEY");
    expect(summary).not.toContain("credential reference is not available");
  });

  it("detects endpoints with CA bootstrap configured", () => {
    expect(hasVoiceboxCaBootstrap({ capabilities: {} })).toBe(false);
    expect(
      hasVoiceboxCaBootstrap({
        capabilities: {
          ca_cert_bootstrap_url:
            "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt"
        }
      })
    ).toBe(true);
  });
});
