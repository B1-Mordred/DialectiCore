export type ProductionActivityAsset = {
  status?: string | null;
  generation_metadata?: Record<string, unknown> | null;
};

export function activeManagedMediaJobCount(
  assets: ProductionActivityAsset[] | null | undefined,
): number {
  return (assets ?? []).filter((asset) => {
    if (!asset || !["submitted", "running"].includes(String(asset.status ?? ""))) {
      return false;
    }
    const metadata = asset.generation_metadata ?? {};
    return Boolean(
      metadata.remote_job_id ||
        metadata.adapter === "b1_managed_media" ||
        metadata.managed_media_api_base,
    );
  }).length;
}
