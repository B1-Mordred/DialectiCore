export type EpisodeEditorState = {
  projectId: string;
  title: string;
  centralQuestion: string;
  targetDurationMinutes: number;
  maximumMonologueSeconds: number;
  permittedDeviationPercent: number;
  hostControl: string;
  allowInterruptions: boolean;
  allowFollowUpQuestions: boolean;
  discussionIntensity: string;
  requiredDimensions: string;
  outputLanguages: string;
  semanticFidelityThreshold: number;
  allowNewClaims: boolean;
  researchSourceTitle: string;
  researchSourceUri: string;
  researchSourceType: string;
  researchSourceContent: string;
  researchDepth: string;
  requireSourceLinks: boolean;
  researchApprovalRequired: boolean;
  mediaWidth: number;
  mediaHeight: number;
  mediaFps: number;
  visualStyle: string;
  cameraStyle: string;
  sceneReferenceImageUri: string;
  subtitleMode: string;
  generateBroll: boolean;
  generateCitationCards: boolean;
  openingEnabled: boolean;
  openingNarrationBrief: string;
  openingSourceReferences: string;
  openingIntroduceParticipants: boolean;
  postPrimerBridgeBrief: string;
  postPrimerBridgeTargetDurationSeconds: number;
  primerNarratorProfileId: string;
  primerTargetDurationSeconds: number;
  primerMediaDiscoveryEnabled: boolean;
  primerMediaModelEndpointId: string;
  primerMediaModelId: string;
  primerMediaMaxCandidates: number;
  primerMediaAcquisitionPolicy: "review_before_download" | "automatic_official_only";
  primerVisualPlannerEnabled: boolean;
  primerVisualPlannerModelEndpointId: string;
  primerVisualPlannerModelId: string;
  primerVisualPlannerAutoRenderDraft: boolean;
  primerVisualPlannerShotDurationSeconds: number;
  primerVisualPlannerAllowGeneratedConnectiveVisuals: boolean;
  primerVisualPlannerExcludePeople: boolean;
  workflowMode: string;
  productionTarget: "native_visual" | "audio_first";
  retryFailedAssets: boolean;
  maximumStageRetries: number;
  blockOnUnsupportedHighImpactClaims: boolean;
  blockOnMissingAudio: boolean;
  blockOnSyncErrorMs: number;
  blockOnMissingSubtitles: boolean;
  hostId: string;
  panelistIds: string[];
  panelistRoles: string[];
};

export const episodeCastRoles = ["panelist", "guest", "fact_checker", "audience_proxy"] as const;

export type EpisodeCastRole = (typeof episodeCastRoles)[number];

export type EpisodeSetupTab =
  | "basics"
  | "research"
  | "primer"
  | "studio"
  | "policy";

export function episodeSetupTabAllowsProductionEdits(
  definitionLocked: boolean,
  tab: EpisodeSetupTab
): boolean {
  return !definitionLocked || tab === "studio";
}

export type EpisodeEditorEpisode = {
  project_id?: string | null;
  title?: string;
  target_duration_seconds?: number;
  definition?: {
    title?: string;
    topic?: {
      central_question?: string;
      required_dimensions?: string[];
    };
    format?: {
      target_duration_minutes?: number;
      permitted_deviation_percent?: number;
      host_control?: string;
      allow_interruptions?: boolean;
      allow_follow_up_questions?: boolean;
      maximum_monologue_seconds?: number;
      discussion_intensity?: string;
    };
    languages?: {
      outputs?: Array<{ language?: string; mode?: string }>;
      semantic_fidelity_threshold?: number;
      allow_new_claims?: boolean;
    };
    research?: {
      depth?: string;
      require_source_links?: boolean;
      approval_required?: boolean;
    };
    media?: {
      width?: number;
      height?: number;
      fps?: number;
      visual_style?: string;
      camera_style?: string;
      scene_reference_image_uri?: string | null;
      subtitle_mode?: string;
      generate_broll?: boolean;
      generate_citation_cards?: boolean;
      directing?: {
        mode?: "studio_directed" | "speaker_only";
        planning_mode?: "auto_with_overrides" | "manual";
        studio_layout?: "seated_panel" | "legacy_overlay";
        seating_plan?: Record<string, number>;
        broll_presentation?: "wall_screen_only" | "disabled";
        require_generated_studio?: boolean;
        require_group_cutaways?: boolean;
        require_reaction_cutaways?: boolean;
        broll_policy?: "contextual_only" | "disabled";
        default_camera_views?: string[];
        allowed_camera_actions?: string[];
      };
      opening?: {
        enabled?: boolean;
        narration_brief?: string;
        source_references?: string[];
        introduce_participants?: boolean;
        post_primer_bridge?: {
          editorial_brief?: string;
          target_duration_seconds?: number;
          introduce_participants?: boolean | null;
        };
        narrator_profile_id?: string | null;
        target_duration_seconds?: number;
        media_discovery?: {
          enabled?: boolean;
          model_endpoint_id?: string | null;
          model_id?: string | null;
          max_candidates?: number;
          acquisition_policy?: "review_before_download" | "automatic_official_only";
        };
        visual_planner?: {
          enabled?: boolean;
          model_endpoint_id?: string | null;
          model_id?: string | null;
          automatic_draft_render?: boolean;
          target_shot_duration_seconds?: number;
          minimum_shot_duration_seconds?: number;
          allow_generated_connective_visuals?: boolean;
          exclude_people?: boolean;
        };
      };
    };
    workflow?: {
      mode?: string;
      production_target?: string;
      retry_failed_assets?: boolean;
      maximum_stage_retries?: number;
    };
    quality?: {
      block_on_unsupported_high_impact_claims?: boolean;
      block_on_missing_audio?: boolean;
      block_on_sync_error_ms?: number;
      block_on_missing_subtitles?: boolean;
    };
    participants?: Array<{
      participant_profile_id?: string;
      role?: string;
    }>;
  };
};

export type EpisodeEditorParticipantOption = {
  id: string;
};

export const defaultEditorState: EpisodeEditorState = {
  projectId: "",
  title: "Frontier KI Pilot: Wer liefert den besten praktischen Nutzen?",
  centralQuestion:
    "Welches Frontier-KI-Modell liefert heute den besten praktischen Nutzen fuer anspruchsvolle Wissensarbeit?",
  targetDurationMinutes: 2,
  maximumMonologueSeconds: 25,
  permittedDeviationPercent: 50,
  hostControl: "high",
  allowInterruptions: false,
  allowFollowUpQuestions: true,
  discussionIntensity: "medium",
  requiredDimensions: "staerken, schwaechen, kosten_nutzen, einsatzempfehlung",
  outputLanguages: "de",
  semanticFidelityThreshold: 0.92,
  allowNewClaims: false,
  researchSourceTitle: "",
  researchSourceUri: "",
  researchSourceType: "manual_source",
  researchSourceContent: "",
  researchDepth: "standard",
  requireSourceLinks: false,
  researchApprovalRequired: false,
  mediaWidth: 1920,
  mediaHeight: 1080,
  mediaFps: 30,
  visualStyle: "studio_realistic",
  cameraStyle: "multi_camera",
  sceneReferenceImageUri: "",
  subtitleMode: "selectable",
  generateBroll: false,
  generateCitationCards: false,
  openingEnabled: true,
  openingNarrationBrief:
    "Explain the topic in plain language using approved evidence: what has happened, why it matters now, and the question the discussion will examine.",
  openingSourceReferences: "",
  openingIntroduceParticipants: false,
  postPrimerBridgeBrief: "",
  postPrimerBridgeTargetDurationSeconds: 35,
  primerNarratorProfileId: "",
  primerTargetDurationSeconds: 60,
  primerMediaDiscoveryEnabled: true,
  primerMediaModelEndpointId: "openrouter",
  primerMediaModelId: "google/gemini-3.6-flash",
  primerMediaMaxCandidates: 6,
  primerMediaAcquisitionPolicy: "review_before_download",
  primerVisualPlannerEnabled: true,
  primerVisualPlannerModelEndpointId: "openrouter",
  primerVisualPlannerModelId: "google/gemini-3.6-flash",
  primerVisualPlannerAutoRenderDraft: true,
  primerVisualPlannerShotDurationSeconds: 6,
  primerVisualPlannerAllowGeneratedConnectiveVisuals: true,
  primerVisualPlannerExcludePeople: true,
  workflowMode: "transcript_review",
  productionTarget: "audio_first",
  retryFailedAssets: true,
  maximumStageRetries: 1,
  blockOnUnsupportedHighImpactClaims: false,
  blockOnMissingAudio: true,
  blockOnSyncErrorMs: 240,
  blockOnMissingSubtitles: false,
  hostId: "claude",
  panelistIds: ["chatgpt", "deepseek", "grok", "gemini", "mistral"],
  panelistRoles: ["panelist", "panelist", "panelist", "panelist", "panelist"]
};

function normalizedEpisodeCastRole(value: string | undefined): EpisodeCastRole {
  return episodeCastRoles.includes(value as EpisodeCastRole)
    ? (value as EpisodeCastRole)
    : "panelist";
}

export function normalizeEpisodeEditorCast(
  editor: EpisodeEditorState,
  hostOptions: EpisodeEditorParticipantOption[],
  panelistOptions: EpisodeEditorParticipantOption[],
  minimumPanelistCount = 5
): EpisodeEditorState {
  const hostId =
    hostOptions.find((profile) => profile.id === editor.hostId)?.id ??
    hostOptions[0]?.id ??
    editor.hostId;
  const panelistOptionIds = new Set(panelistOptions.map((profile) => profile.id));
  const selectedPanelistIds = new Set<string>();
  const targetPanelistCount = Math.min(
    Math.max(editor.panelistIds.length, minimumPanelistCount),
    panelistOptions.length
  );
  const panelistIds: string[] = [];
  const panelistRoles: EpisodeCastRole[] = [];

  for (const [index, id] of editor.panelistIds.entries()) {
    if (id === hostId || !panelistOptionIds.has(id) || selectedPanelistIds.has(id)) {
      continue;
    }
    selectedPanelistIds.add(id);
    panelistIds.push(id);
    panelistRoles.push(normalizedEpisodeCastRole(editor.panelistRoles[index]));
  }

  for (const option of panelistOptions) {
    if (panelistIds.length >= targetPanelistCount) {
      break;
    }
    if (option.id === hostId || selectedPanelistIds.has(option.id)) {
      continue;
    }
    selectedPanelistIds.add(option.id);
    panelistIds.push(option.id);
    panelistRoles.push("panelist");
  }

  return {
    ...editor,
    hostId,
    panelistIds,
    panelistRoles
  };
}

export function episodeEditorStateFromEpisode(
  episode: EpisodeEditorEpisode,
  fallback: EpisodeEditorState = defaultEditorState
): EpisodeEditorState {
  const definition = episode.definition ?? {};
  const format = definition.format ?? {};
  const topic = definition.topic ?? {};
  const languages = definition.languages ?? {};
  const research = definition.research ?? {};
  const media = definition.media ?? {};
  const workflow = definition.workflow ?? {};
  const quality = definition.quality ?? {};
  const opening = media.opening;
  const openingIntroduceParticipants =
    opening?.post_primer_bridge?.introduce_participants ??
    opening?.introduce_participants ??
    fallback.openingIntroduceParticipants;
  const configuredBridgeDuration =
    opening?.post_primer_bridge?.target_duration_seconds ??
    fallback.postPrimerBridgeTargetDurationSeconds;
  const participants = definition.participants ?? [];
  const hostId =
    participants.find((participant) => participant.role === "moderator")
      ?.participant_profile_id ?? fallback.hostId;
  const nonModeratorParticipants = participants.filter(
    (participant) => participant.role !== "moderator"
  );
  const panelistIds = nonModeratorParticipants
    .map((participant) => participant.participant_profile_id)
    .filter((id): id is string => Boolean(id));
  const outputLanguages = (languages.outputs ?? [])
    .map((output) => output.language)
    .filter((language): language is string => Boolean(language));

  return {
    ...fallback,
    projectId: episode.project_id ?? "",
    title: definition.title ?? episode.title ?? fallback.title,
    centralQuestion: topic.central_question ?? fallback.centralQuestion,
    targetDurationMinutes:
      format.target_duration_minutes ??
      (episode.target_duration_seconds
        ? Math.max(1, Math.round(episode.target_duration_seconds / 60))
        : fallback.targetDurationMinutes),
    maximumMonologueSeconds:
      format.maximum_monologue_seconds ?? fallback.maximumMonologueSeconds,
    permittedDeviationPercent:
      format.permitted_deviation_percent ?? fallback.permittedDeviationPercent,
    hostControl: format.host_control ?? fallback.hostControl,
    allowInterruptions: format.allow_interruptions ?? fallback.allowInterruptions,
    allowFollowUpQuestions:
      format.allow_follow_up_questions ?? fallback.allowFollowUpQuestions,
    discussionIntensity: format.discussion_intensity ?? fallback.discussionIntensity,
    requiredDimensions:
      topic.required_dimensions && topic.required_dimensions.length > 0
        ? topic.required_dimensions.join(", ")
        : fallback.requiredDimensions,
    outputLanguages:
      outputLanguages.length > 0 ? outputLanguages.join(", ") : fallback.outputLanguages,
    semanticFidelityThreshold:
      languages.semantic_fidelity_threshold ?? fallback.semanticFidelityThreshold,
    allowNewClaims: languages.allow_new_claims ?? fallback.allowNewClaims,
    researchDepth: research.depth ?? fallback.researchDepth,
    requireSourceLinks: research.require_source_links ?? fallback.requireSourceLinks,
    researchApprovalRequired:
      research.approval_required ?? fallback.researchApprovalRequired,
    mediaWidth: media.width ?? fallback.mediaWidth,
    mediaHeight: media.height ?? fallback.mediaHeight,
    mediaFps: media.fps ?? fallback.mediaFps,
    visualStyle: media.visual_style ?? fallback.visualStyle,
    cameraStyle: media.camera_style ?? fallback.cameraStyle,
    sceneReferenceImageUri: media.scene_reference_image_uri ?? "",
    subtitleMode: media.subtitle_mode ?? fallback.subtitleMode,
    generateBroll: media.generate_broll ?? fallback.generateBroll,
    generateCitationCards:
      media.generate_citation_cards ?? fallback.generateCitationCards,
    openingEnabled: opening?.enabled ?? fallback.openingEnabled,
    openingNarrationBrief: opening?.narration_brief ?? fallback.openingNarrationBrief,
    openingSourceReferences: (opening?.source_references ?? []).join(", "),
    openingIntroduceParticipants,
    postPrimerBridgeBrief:
      opening?.post_primer_bridge?.editorial_brief ?? fallback.postPrimerBridgeBrief,
    postPrimerBridgeTargetDurationSeconds: openingIntroduceParticipants
      ? Math.max(configuredBridgeDuration, 35)
      : configuredBridgeDuration,
    primerNarratorProfileId: opening?.narrator_profile_id ?? fallback.primerNarratorProfileId,
    primerTargetDurationSeconds:
      opening?.target_duration_seconds ?? fallback.primerTargetDurationSeconds,
    primerMediaDiscoveryEnabled:
      media.opening?.media_discovery?.enabled ?? fallback.primerMediaDiscoveryEnabled,
    primerMediaModelEndpointId:
      media.opening?.media_discovery?.model_endpoint_id ?? fallback.primerMediaModelEndpointId,
    primerMediaModelId:
      media.opening?.media_discovery?.model_id ?? fallback.primerMediaModelId,
    primerMediaMaxCandidates:
      media.opening?.media_discovery?.max_candidates ?? fallback.primerMediaMaxCandidates,
    primerMediaAcquisitionPolicy:
      media.opening?.media_discovery?.acquisition_policy ??
      fallback.primerMediaAcquisitionPolicy,
    primerVisualPlannerEnabled:
      media.opening?.visual_planner?.enabled ?? fallback.primerVisualPlannerEnabled,
    primerVisualPlannerModelEndpointId:
      media.opening?.visual_planner?.model_endpoint_id ??
      fallback.primerVisualPlannerModelEndpointId,
    primerVisualPlannerModelId:
      media.opening?.visual_planner?.model_id ?? fallback.primerVisualPlannerModelId,
    primerVisualPlannerAutoRenderDraft:
      media.opening?.visual_planner?.automatic_draft_render ??
      fallback.primerVisualPlannerAutoRenderDraft,
    primerVisualPlannerShotDurationSeconds:
      media.opening?.visual_planner?.target_shot_duration_seconds ??
      fallback.primerVisualPlannerShotDurationSeconds,
    primerVisualPlannerAllowGeneratedConnectiveVisuals:
      media.opening?.visual_planner?.allow_generated_connective_visuals ??
      fallback.primerVisualPlannerAllowGeneratedConnectiveVisuals,
    primerVisualPlannerExcludePeople:
      media.opening?.visual_planner?.exclude_people ?? fallback.primerVisualPlannerExcludePeople,
    workflowMode: workflow.mode ?? fallback.workflowMode,
    productionTarget:
      workflow.production_target === "native_visual" ||
      workflow.production_target === "audio_first"
        ? workflow.production_target
        : fallback.productionTarget,
    retryFailedAssets: workflow.retry_failed_assets ?? fallback.retryFailedAssets,
    maximumStageRetries:
      workflow.maximum_stage_retries ?? fallback.maximumStageRetries,
    blockOnUnsupportedHighImpactClaims:
      quality.block_on_unsupported_high_impact_claims ??
      fallback.blockOnUnsupportedHighImpactClaims,
    blockOnMissingAudio: quality.block_on_missing_audio ?? fallback.blockOnMissingAudio,
    blockOnSyncErrorMs: quality.block_on_sync_error_ms ?? fallback.blockOnSyncErrorMs,
    blockOnMissingSubtitles:
      quality.block_on_missing_subtitles ?? fallback.blockOnMissingSubtitles,
    hostId,
    panelistIds: panelistIds.length > 0 ? panelistIds : fallback.panelistIds,
    panelistRoles:
      panelistIds.length > 0
        ? nonModeratorParticipants.map((participant) => normalizedEpisodeCastRole(participant.role))
        : fallback.panelistRoles
  };
}
