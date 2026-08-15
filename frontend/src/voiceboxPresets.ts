export const b1VoiceboxEndpointCapabilities = {
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
  ca_cert_sha256: "326339ee673c53ecca71b7006b177996dbec2c5166b94b2e3cfd56060612bb0c",
  tls_ca_cert_path: "/data/runtime-state/certificates/b1-ai-hub-caddy-root.crt"
};

export const b1LocalVoiceboxBridgeEndpointCapabilities = {
  tts: true,
  stream_generation_path: "/generate/stream",
  response_mode: "audio_stream",
  accept: "audio/wav",
  default_engine: "remote_http",
  voice_profile_engine: "remote_http",
  voice_profile_id_prefix: "bridge-",
  credential_required: false,
  generation_canary_enabled: true,
  generation_canary_profile_id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
  generation_canary_engine: "remote_http",
  generation_canary_language: "de",
  generation_canary_text: "Guten Tag. DialectiCore prueft die Stimme.",
  generation_canary_timeout_seconds: 20,
  normalize_default: false,
  effects_chain_default: [],
  formats: ["audio/wav"],
  expected_sample_rates: [24000],
  postprocess_audio_loudness: true,
  audio_postprocess_timeout_seconds: 30,
  audio_postprocess_true_peak_limit_dbtp: -2.5,
  estimate_word_timestamps_from_text: true,
  local_voicebox_profile_ids: {
    "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5": "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
    "67a00466-17ba-4f26-812e-60c13119be9e": "67a00466-17ba-4f26-812e-60c13119be9e",
    "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4": "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
    "1418bd8c-1c39-4317-91a0-92d62e5fd9c0": "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
    "7476947f-5836-480b-9a95-67bf66575c2a": "7476947f-5836-480b-9a95-67bf66575c2a",
    "1865b646-41ca-4140-ba9d-1a40d9fe623a": "8c54e9a6-f5a6-4d02-9706-e7403c80ea72"
  }
};

export const b1GermanVoiceProfiles = [
  {
    id: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
    name: "A_DE_Claude",
    voice_id: "bd4e9bf1-482b-4900-97c1-48275d1ba28c"
  },
  {
    id: "67a00466-17ba-4f26-812e-60c13119be9e",
    name: "A_DE_DeepSeek",
    voice_id: "85ff8fac-a53c-4385-a1d5-5930d3a142aa"
  },
  {
    id: "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
    name: "A_DE_Grok",
    voice_id: "3eee56a8-119b-4ed8-b829-72efe51a8be6"
  },
  {
    id: "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
    name: "A_DE_Gemini",
    voice_id: "6e70eabc-ee1c-4a5d-a488-4094d4384507"
  },
  {
    id: "7476947f-5836-480b-9a95-67bf66575c2a",
    name: "A_DE_Mistral",
    voice_id: "4e2a637d-575d-446e-b3d6-7141d655a4e6"
  },
  {
    id: "1865b646-41ca-4140-ba9d-1a40d9fe623a",
    name: "A_ChatGPT",
    voice_id: "1865b646-41ca-4140-ba9d-1a40d9fe623a"
  }
] as const;

export type B1GermanVoiceProfilePreset = (typeof b1GermanVoiceProfiles)[number];

export const b1CharacterVoiceAssignments = {
  chatgpt: "1865b646-41ca-4140-ba9d-1a40d9fe623a",
  claude: "0e0b05f9-7f11-4f81-ad4c-d3c664f5ccb5",
  deepseek: "67a00466-17ba-4f26-812e-60c13119be9e",
  gemini: "1418bd8c-1c39-4317-91a0-92d62e5fd9c0",
  grok: "9b327c5c-ecb4-4f76-8fa8-25214d21e2c4",
  mistral: "7476947f-5836-480b-9a95-67bf66575c2a"
} as const;

export function b1GermanVoiceProfilePayload(
  voice: B1GermanVoiceProfilePreset,
  voiceboxEndpointId = "b1-voicebox",
): Record<string, unknown> {
  return {
    id: voice.id,
    name: voice.name,
    voicebox_endpoint_id: voiceboxEndpointId,
    voice_id: voice.voice_id,
    language: "de",
    speaker_label: voice.name,
    model_id: "chatterbox",
    prosody: { engine: "chatterbox", normalize: false, effects_chain: [] },
    rate: 1,
    pitch: 0,
    pronunciation_dictionary: {},
    enabled: true
  };
}

export function missingB1GermanVoiceProfiles(
  existingProfiles: Array<{ id: string }>,
): B1GermanVoiceProfilePreset[] {
  const existingIds = new Set(existingProfiles.map((profile) => profile.id));
  return b1GermanVoiceProfiles.filter((preset) => !existingIds.has(preset.id));
}

export function b1ParticipantVoiceAssignments(
  participants: Array<{ id: string; voice_profile_id?: string | null }>,
  existingProfiles: Array<{ id: string }>,
): Array<{ participantId: string; voiceProfileId: string }> {
  const existingProfileIds = new Set(existingProfiles.map((profile) => profile.id));
  const assignedVoiceIds = new Set(
    participants
      .map((participant) => participant.voice_profile_id)
      .filter((voiceId): voiceId is string => typeof voiceId === "string" && voiceId.length > 0)
  );

  return participants.flatMap((participant) => {
    if (participant.voice_profile_id) {
      return [];
    }
    const voiceProfileId =
      b1CharacterVoiceAssignments[
        participant.id as keyof typeof b1CharacterVoiceAssignments
      ];
    if (
      !voiceProfileId ||
      !existingProfileIds.has(voiceProfileId) ||
      assignedVoiceIds.has(voiceProfileId)
    ) {
      return [];
    }
    return [{ participantId: participant.id, voiceProfileId }];
  });
}
