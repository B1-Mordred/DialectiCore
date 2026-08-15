export type PilotReadinessAction = {
  key: "characters";
  label: string;
  reason: string;
};

export type PilotReadinessForActions = {
  blockers?: string[];
  all_stage_blockers?: string[];
  stages?: Array<{
    category?: string;
    status?: string;
    blockers?: string[];
    details?: Record<string, unknown>;
  }>;
};

const CHARACTER_BLOCKER_PATTERNS = [
  "selected participants need non-mock model",
  "selected participants need non-mock voicebox",
  "selected participants are missing visual profiles",
  "selected participants reference missing visual profiles",
  "selected participants use disabled visual profiles",
  "selected characters are missing portrait",
  "selected characters are missing full-body",
  "selected visuals reference missing comfyui workflows",
  "selected visuals use disabled comfyui workflows",
];

const CHARACTER_FAILED_CHECKS = new Set([
  "selected_participants_have_remote_models",
  "selected_participants_have_remote_voices",
  "selected_participants_have_visual_profiles",
  "selected_visual_profiles_exist",
  "selected_visual_profiles_enabled",
  "selected_visual_profiles_have_portraits",
  "selected_visual_profiles_have_full_body_references",
  "selected_visual_workflows_exist",
  "selected_visual_workflows_enabled",
]);

export function pilotReadinessAction(
  readiness: PilotReadinessForActions | null | undefined
): PilotReadinessAction | null {
  if (!readiness) {
    return null;
  }
  const blockers = [
    ...(readiness.blockers ?? []),
    ...(readiness.all_stage_blockers ?? []),
    ...(readiness.stages ?? []).flatMap((stage) => stage.blockers ?? []),
  ];
  const directBlocker = blockers.find(isCharacterBlocker);
  if (directBlocker) {
    return {
      key: "characters",
      label: "Open Characters",
      reason: directBlocker,
    };
  }
  const failedCheck = (readiness.stages ?? [])
    .flatMap((stage) => failedReadinessChecks(stage.details))
    .find((check) => CHARACTER_FAILED_CHECKS.has(check));
  if (failedCheck) {
    return {
      key: "characters",
      label: "Open Characters",
      reason: failedCheck,
    };
  }
  return null;
}

function isCharacterBlocker(value: string): boolean {
  const normalized = value.toLowerCase();
  return CHARACTER_BLOCKER_PATTERNS.some((pattern) => normalized.includes(pattern));
}

function failedReadinessChecks(details: Record<string, unknown> | undefined): string[] {
  const value = details?.failed_readiness_checks;
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === "string") : [];
}
