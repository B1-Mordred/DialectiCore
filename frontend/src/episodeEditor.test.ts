import { describe, expect, it } from "vitest";
import {
  defaultEditorState,
  episodeSetupTabAllowsProductionEdits,
  episodeEditorStateFromEpisode,
  normalizeEpisodeEditorCast,
} from "./episodeEditor";

describe("episode editor state", () => {
  it("keeps studio production settings reachable after the definition is locked", () => {
    expect(episodeSetupTabAllowsProductionEdits(true, "studio")).toBe(true);
    expect(episodeSetupTabAllowsProductionEdits(true, "basics")).toBe(false);
    expect(episodeSetupTabAllowsProductionEdits(true, "primer")).toBe(false);
    expect(episodeSetupTabAllowsProductionEdits(false, "basics")).toBe(true);
  });

  it("loads selected episode definition fields into the editor", () => {
    const editor = episodeEditorStateFromEpisode({
      project_id: "project-a",
      title: "Persisted title",
      target_duration_seconds: 180,
      definition: {
        title: "Definition title",
        topic: {
          central_question: "What should the panel debate?",
          required_dimensions: ["evidence", "cost", "deployment"]
        },
        format: {
          target_duration_minutes: 3,
          permitted_deviation_percent: 25,
          host_control: "medium",
          allow_interruptions: true,
          allow_follow_up_questions: false,
          maximum_monologue_seconds: 35,
          discussion_intensity: "high"
        },
        languages: {
          outputs: [
            { language: "de", mode: "canonical" },
            { language: "en", mode: "localized_reperformance" }
          ],
          semantic_fidelity_threshold: 0.88,
          allow_new_claims: true
        },
        research: {
          depth: "deep",
          require_source_links: true,
          approval_required: true
        },
        media: {
          width: 1280,
          height: 720,
          fps: 25,
          visual_style: "newsroom",
          camera_style: "panel",
          scene_reference_image_uri:
            "object://dialecticore/show-media/scene-reference-images/studio.png",
          subtitle_mode: "burned_in",
          generate_broll: true,
          generate_citation_cards: true
        },
        workflow: {
          mode: "full_auto",
          production_target: "native_visual",
          retry_failed_assets: false,
          maximum_stage_retries: 4
        },
        quality: {
          block_on_unsupported_high_impact_claims: true,
          block_on_missing_audio: false,
          block_on_sync_error_ms: 120,
          block_on_missing_subtitles: true
        },
        participants: [
          { participant_profile_id: "claude", role: "moderator" },
          { participant_profile_id: "chatgpt", role: "panelist" },
          { participant_profile_id: "deepseek", role: "panelist" }
        ]
      }
    });

    expect(editor).toMatchObject({
      projectId: "project-a",
      title: "Definition title",
      centralQuestion: "What should the panel debate?",
      targetDurationMinutes: 3,
      maximumMonologueSeconds: 35,
      permittedDeviationPercent: 25,
      hostControl: "medium",
      allowInterruptions: true,
      allowFollowUpQuestions: false,
      discussionIntensity: "high",
      requiredDimensions: "evidence, cost, deployment",
      outputLanguages: "de, en",
      semanticFidelityThreshold: 0.88,
      allowNewClaims: true,
      researchDepth: "deep",
      requireSourceLinks: true,
      researchApprovalRequired: true,
      mediaWidth: 1280,
      mediaHeight: 720,
      mediaFps: 25,
      visualStyle: "newsroom",
      cameraStyle: "panel",
      sceneReferenceImageUri:
        "object://dialecticore/show-media/scene-reference-images/studio.png",
      subtitleMode: "burned_in",
      generateBroll: true,
      generateCitationCards: true,
      workflowMode: "full_auto",
      productionTarget: "native_visual",
      retryFailedAssets: false,
      maximumStageRetries: 4,
      blockOnUnsupportedHighImpactClaims: true,
      blockOnMissingAudio: false,
      blockOnSyncErrorMs: 120,
      blockOnMissingSubtitles: true,
      hostId: "claude",
      panelistIds: ["chatgpt", "deepseek"],
      panelistRoles: ["panelist", "panelist"]
    });
  });

  it("preserves fallback fields when older episodes lack newer definition keys", () => {
    const editor = episodeEditorStateFromEpisode(
      {
        title: "Legacy title",
        target_duration_seconds: 90,
        definition: {
          languages: { outputs: [] }
        }
      },
      {
        ...defaultEditorState,
        hostId: "existing-host",
        panelistIds: ["existing-panelist"],
        researchSourceTitle: "draft-only source"
      }
    );

    expect(editor.title).toBe("Legacy title");
    expect(editor.targetDurationMinutes).toBe(2);
    expect(editor.hostId).toBe("existing-host");
    expect(editor.panelistIds).toEqual(["existing-panelist"]);
    expect(editor.researchSourceTitle).toBe("draft-only source");
    expect(editor.outputLanguages).toBe(defaultEditorState.outputLanguages);
  });

  it("repairs duplicate panelists and fills the frontier cast with unique participants", () => {
    const editor = normalizeEpisodeEditorCast(
      {
        ...defaultEditorState,
        hostId: "claude",
        panelistIds: ["chatgpt", "chatgpt", "chatgpt"]
      },
      [{ id: "claude" }],
      [
        { id: "chatgpt" },
        { id: "deepseek" },
        { id: "grok" },
        { id: "gemini" },
        { id: "mistral" }
      ]
    );

    expect(editor.hostId).toBe("claude");
    expect(editor.panelistIds).toEqual(["chatgpt", "deepseek", "grok", "gemini", "mistral"]);
  });

  it("removes the selected host from panelist slots", () => {
    const editor = normalizeEpisodeEditorCast(
      {
        ...defaultEditorState,
        hostId: "claude",
        panelistIds: ["claude", "chatgpt", "deepseek", "deepseek"]
      },
      [{ id: "claude" }],
      [
        { id: "claude" },
        { id: "chatgpt" },
        { id: "deepseek" },
        { id: "grok" },
        { id: "gemini" },
        { id: "mistral" }
      ]
    );

    expect(editor.panelistIds).toEqual(["chatgpt", "deepseek", "grok", "gemini", "mistral"]);
  });

  it("preserves episode-scoped cast roles while repairing the selected cast", () => {
    const editor = normalizeEpisodeEditorCast(
      {
        ...defaultEditorState,
        hostId: "chatgpt",
        panelistIds: ["claude", "deepseek", "grok"],
        panelistRoles: ["guest", "fact_checker", "audience_proxy"]
      },
      [{ id: "chatgpt" }, { id: "claude" }, { id: "deepseek" }, { id: "grok" }],
      [{ id: "chatgpt" }, { id: "claude" }, { id: "deepseek" }, { id: "grok" }],
      3
    );

    expect(editor.hostId).toBe("chatgpt");
    expect(editor.panelistIds).toEqual(["claude", "deepseek", "grok"]);
    expect(editor.panelistRoles).toEqual(["guest", "fact_checker", "audience_proxy"]);
  });
});
