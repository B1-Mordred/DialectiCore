export type ProductionReportOperatorActionEntry = {
  scope?: string;
  action?: string;
  status?: string | null;
  failed_check_count?: number;
  asset_count?: number;
};

export type ProductionReportOperatorAsset = {
  download_url?: string | null;
  downloadable?: boolean;
};

export type ProductionReportForOperatorActions = {
  status?: string | null;
  operator_next_action?: string | null;
  operator_next_actions?: ProductionReportOperatorActionEntry[];
  blockers?: string[];
  completion?: {
    failed_checks?: string[];
  };
  deliverables?: {
    final_render?: ProductionReportOperatorAsset | null;
    export_package?: ProductionReportOperatorAsset | null;
    production_manifest?: ProductionReportOperatorAsset | null;
  };
};

export type ProductionReportOperatorActionSummary = {
  actions: string[];
  showApprovals: boolean;
  showTranscriptReview: boolean;
  showPublishDryRun: boolean;
  showDeliverableDownloads: boolean;
};

const APPROVAL_CHECKS = new Set([
  "research_approval_missing",
  "research_approval_rejected",
  "preview_render_approval_missing",
  "final_render_approval_missing",
  "localized_output_not_approved",
]);

const APPROVAL_ACTIONS = new Set([
  "approve_or_generate_broadcast_transcript",
  "approve_broadcast_transcript",
  "review_preview_render",
  "review_final_render",
  "approve_localized_outputs",
]);

export function productionReportOperatorActionSummary(
  report: ProductionReportForOperatorActions | null | undefined
): ProductionReportOperatorActionSummary {
  if (!report) {
    return {
      actions: [],
      showApprovals: false,
      showTranscriptReview: false,
      showPublishDryRun: false,
      showDeliverableDownloads: false,
    };
  }
  const actions = normalizedActions(report);
  const checks = [...(report.blockers ?? []), ...(report.completion?.failed_checks ?? [])];
  const showTranscriptReview = actions.some(isTranscriptReviewAction);
  return {
    actions,
    showApprovals: showTranscriptReview || checks.some(isApprovalCheck) || actions.some(isApprovalAction),
    showTranscriptReview,
    showPublishDryRun: actions.includes("run_dry_run_publish_for_real_life_test"),
    showDeliverableDownloads:
      actions.includes("inspect_export_package_and_publish_evidence") &&
      hasAnyDownloadableDeliverable(report),
  };
}

function normalizedActions(report: ProductionReportForOperatorActions): string[] {
  const values = [
    report.operator_next_action ?? undefined,
    ...(report.operator_next_actions ?? []).map((entry) => entry.action),
  ].filter((value): value is string => typeof value === "string" && value.length > 0);
  return Array.from(new Set(values));
}

function isApprovalCheck(value: string): boolean {
  return APPROVAL_CHECKS.has(value) || value.endsWith("_approval_missing");
}

function isApprovalAction(value: string): boolean {
  return APPROVAL_ACTIONS.has(value);
}

function isTranscriptReviewAction(value: string): boolean {
  return (
    value === "approve_broadcast_transcript" ||
    value === "approve_or_generate_broadcast_transcript"
  );
}

function hasAnyDownloadableDeliverable(report: ProductionReportForOperatorActions): boolean {
  const deliverables = report.deliverables;
  return Boolean(
    deliverables?.final_render?.downloadable ||
      deliverables?.export_package?.downloadable ||
      deliverables?.production_manifest?.downloadable
  );
}
