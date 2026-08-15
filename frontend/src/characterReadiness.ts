export type CharacterReadinessParticipant = {
  model_endpoint_id?: string | null;
  model_id?: string | null;
  voice_profile_id?: string | null;
  visual_profile_id?: string | null;
};

export type CharacterReadinessVisualProfile = {
  reference_image_uri?: string | null;
  reference_images?: Array<{
    reference_type?: string | null;
    uri?: string | null;
  }>;
};

export type CharacterReadinessBadge = {
  key: "model" | "voice" | "portrait" | "full_body";
  label: string;
  ready: boolean;
  title: string;
};

export function characterReadinessBadges(
  participant: CharacterReadinessParticipant,
  visualProfile: CharacterReadinessVisualProfile | null | undefined
): CharacterReadinessBadge[] {
  return [
    {
      key: "model",
      label: "M",
      ready: Boolean(participant.model_endpoint_id && participant.model_id),
      title: participant.model_endpoint_id && participant.model_id
        ? "Model configured"
        : "Missing model endpoint or model ID",
    },
    {
      key: "voice",
      label: "V",
      ready: Boolean(participant.voice_profile_id),
      title: participant.voice_profile_id ? "Voice assigned" : "Missing voice profile",
    },
    {
      key: "portrait",
      label: "P",
      ready: hasVisualReference(visualProfile, "portrait"),
      title: hasVisualReference(visualProfile, "portrait")
        ? "Portrait reference uploaded"
        : "Missing portrait reference",
    },
    {
      key: "full_body",
      label: "F",
      ready: hasVisualReference(visualProfile, "full_body"),
      title: hasVisualReference(visualProfile, "full_body")
        ? "Full-body reference uploaded"
        : "Missing full-body reference",
    },
  ];
}

export function characterReadinessSummary(
  participant: CharacterReadinessParticipant,
  visualProfile: CharacterReadinessVisualProfile | null | undefined
): string {
  const missing = characterReadinessBadges(participant, visualProfile)
    .filter((badge) => !badge.ready)
    .map((badge) => badge.title.toLowerCase());
  return missing.length === 0 ? "production-ready character setup" : missing.join("; ");
}

export function hasVisualReference(
  visualProfile: CharacterReadinessVisualProfile | null | undefined,
  referenceType: "portrait" | "full_body"
): boolean {
  if (!visualProfile) {
    return false;
  }
  if (referenceType === "portrait" && visualProfile.reference_image_uri) {
    return true;
  }
  return Boolean(
    (visualProfile.reference_images ?? []).some(
      (reference) => reference.reference_type === referenceType && reference.uri
    )
  );
}
