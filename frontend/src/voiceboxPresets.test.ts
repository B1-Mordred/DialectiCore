import { describe, expect, it } from "vitest";
import {
  b1LocalVoiceboxBridgeEndpointCapabilities,
  b1ParticipantVoiceAssignments,
  b1GermanVoiceProfilePayload,
  b1GermanVoiceProfiles,
  b1VoiceboxEndpointCapabilities,
  missingB1GermanVoiceProfiles,
} from "./voiceboxPresets";

const b1BearerPrefix = ["b1", "k_"].join("");

describe("B1 Voicebox presets", () => {
  it("captures the native stream endpoint contract without a raw bearer token", () => {
    expect(b1VoiceboxEndpointCapabilities).toEqual({
      tts: true,
      stream_generation_path: "/generate/stream",
      response_mode: "audio_stream",
      accept: "audio/wav",
      default_engine: "chatterbox",
      normalize_default: false,
      effects_chain_default: [],
      transcription_base_url: "https://api.ai.b1.germering",
      transcription_path: "/v1/audio/transcriptions",
      transcription_model: "stt-default",
      transcription_timeout_seconds: 120,
      transcription_qc_max_attempts: 2,
      generation_canary_enabled: true,
      generation_canary_profile_id: "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
      generation_canary_engine: "chatterbox",
      generation_canary_language: "de",
      generation_canary_text: "Guten Tag. DialectiCore prueft die Stimme.",
      generation_canary_timeout_seconds: 20,
      require_base_url_dns_resolution: true,
      formats: ["audio/wav"],
      ca_cert_bootstrap_url:
        "https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt",
      ca_cert_sha256:
        "326339ee673c53ecca71b7006b177996dbec2c5166b94b2e3cfd56060612bb0c",
      tls_ca_cert_path:
        "/data/runtime-state/certificates/b1-ai-hub-caddy-root.crt",
    });
    expect(JSON.stringify(b1VoiceboxEndpointCapabilities)).not.toContain(
      b1BearerPrefix,
    );
  });

  it("captures the local Voicebox bridge mapping for remote-backed smoke tests", () => {
    expect(b1LocalVoiceboxBridgeEndpointCapabilities).toMatchObject({
      tts: true,
      stream_generation_path: "/generate/stream",
      response_mode: "audio_stream",
      default_engine: "remote_http",
      voice_profile_engine: "remote_http",
      voice_profile_id_prefix: "bridge-",
      credential_required: false,
      expected_sample_rates: [24000],
      local_voicebox_profile_ids: {
        "1865b646-41ca-4140-ba9d-1a40d9fe623a":
          "8c54e9a6-f5a6-4d02-9706-e7403c80ea72",
        "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5":
          "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
      },
    });
    expect(
      JSON.stringify(b1LocalVoiceboxBridgeEndpointCapabilities),
    ).not.toContain(b1BearerPrefix);
  });

  it("captures every supplied German remote native voice profile UUID", () => {
    expect(b1GermanVoiceProfiles).toEqual([
      {
        id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
        name: "A_DE_Claude",
        voice_id: "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
      },
      {
        id: "67a00466-17ba-4f26-812e-60c13119be9e",
        name: "A_DE_DeepSeek",
        voice_id: "85ff8fac-a53c-4385-a1d5-5930d3a142aa",
      },
      {
        id: "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
        name: "A_DE_Grok",
        voice_id: "3eee56a8-119b-4ed8-b829-72efe51a8be6",
      },
      {
        id: "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
        name: "A_DE_Gemini",
        voice_id: "6e70eabc-ee1c-4a5d-a488-4094d4384507",
      },
      {
        id: "7476947f-5836-480b-9a95-67bf66575c2a",
        name: "A_DE_Mistral",
        voice_id: "4e2a637d-575d-446e-b3d6-7141d655a4e6",
      },
      {
        id: "1865b646-41ca-4140-ba9d-1a40d9fe623a",
        name: "A_ChatGPT",
        voice_id: "1865b646-41ca-4140-ba9d-1a40d9fe623a",
      },
    ]);
  });

  it("builds normal voice profile payloads for bulk preset creation", () => {
    expect(b1GermanVoiceProfilePayload(b1GermanVoiceProfiles[0])).toEqual({
      id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
      name: "A_DE_Claude",
      voicebox_endpoint_id: "b1-voicebox",
      voice_id: "bd4e9bf1-482b-4900-97c1-48275d1ba28c",
      language: "de",
      speaker_label: "A_DE_Claude",
      model_id: "chatterbox",
      prosody: { engine: "chatterbox", normalize: false, effects_chain: [] },
      rate: 1,
      pitch: 0,
      pronunciation_dictionary: {},
      enabled: true,
    });
  });

  it("selects only missing B1 voices so existing edited profiles are preserved", () => {
    const missing = missingB1GermanVoiceProfiles([
      { id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5" },
      { id: "1418bd8c-1c39-4317-91a0-92d62e5fd9c0" },
      { id: "operator-custom-voice" },
    ]);

    expect(missing.map((voice) => voice.name)).toEqual([
      "A_DE_DeepSeek",
      "A_DE_Grok",
      "A_DE_Mistral",
      "A_ChatGPT",
    ]);
  });

  it("assigns saved B1 voices to matching unassigned frontier characters", () => {
    const assignments = b1ParticipantVoiceAssignments(
      [
        { id: "chatgpt", voice_profile_id: "" },
        { id: "claude", voice_profile_id: null },
        { id: "deepseek" },
        { id: "gemini", voice_profile_id: "custom-gemini" },
        { id: "grok" },
        { id: "mistral" },
        { id: "host", voice_profile_id: "" },
      ],
      [
        { id: "custom-voice" },
        { id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5" },
        { id: "67a00466-17ba-4f26-812e-60c13119be9e" },
        { id: "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4" },
        { id: "7476947f-5836-480b-9a95-67bf66575c2a" },
        { id: "1865b646-41ca-4140-ba9d-1a40d9fe623a" },
      ],
    );

    expect(assignments).toEqual([
      {
        participantId: "chatgpt",
        voiceProfileId: "1865b646-41ca-4140-ba9d-1a40d9fe623a",
      },
      {
        participantId: "claude",
        voiceProfileId: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
      },
      {
        participantId: "deepseek",
        voiceProfileId: "67a00466-17ba-4f26-812e-60c13119be9e",
      },
      {
        participantId: "grok",
        voiceProfileId: "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
      },
      {
        participantId: "mistral",
        voiceProfileId: "7476947f-5836-480b-9a95-67bf66575c2a",
      },
    ]);
  });
});
