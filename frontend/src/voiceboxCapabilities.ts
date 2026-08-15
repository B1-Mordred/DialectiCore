export function hasVoiceboxCaBootstrap(endpoint: {
  capabilities?: Record<string, unknown>;
}): boolean {
  const value = endpoint.capabilities?.ca_cert_bootstrap_url;
  return typeof value === "string" && value.trim().length > 0;
}

export function formatVoiceboxCapabilities(capabilities: Record<string, unknown>): string {
  const formats = Array.isArray(capabilities.formats)
    ? capabilities.formats.join(", ")
    : "formats unknown";
  const responseMode =
    capabilities.response_mode === "audio_stream" ? "stream audio" : "json metadata";
  const timestamps = capabilities.word_timestamps ? "word timing" : "no word timing";
  const transcriptionQc =
    typeof capabilities.transcription_base_url === "string"
      ? `speech QC: ${String(capabilities.transcription_model ?? "stt-default")}`
      : "";
  const credentialStatus = formatCredentialStatus(capabilities);
  const caStatus = formatCaStatus(capabilities);
  return [formats, responseMode, timestamps, transcriptionQc, credentialStatus, caStatus]
    .filter(Boolean)
    .join(" · ");
}

function formatCredentialStatus(capabilities: Record<string, unknown>): string {
  if (typeof capabilities.credential_reference_configured !== "boolean") {
    return "";
  }
  if (capabilities.credential_reference_configured === false) {
    return "credential missing";
  }
  if (capabilities.credential_reference_resolved === true) {
    return "credential resolved";
  }
  if (capabilities.credential_reference_resolved === false) {
    return "credential unavailable";
  }
  return "credential unchecked";
}

function formatCaStatus(capabilities: Record<string, unknown>): string {
  if (typeof capabilities.ca_cert_bootstrap_url !== "string") {
    return "";
  }
  const bootstrap =
    capabilities.ca_cert_bootstrap && typeof capabilities.ca_cert_bootstrap === "object"
      ? (capabilities.ca_cert_bootstrap as Record<string, unknown>)
      : {};
  const parts = [
    capabilities.tls_ca_cert_available === true
      ? "CA file ready"
      : capabilities.tls_ca_cert_available === false
        ? "CA file missing"
        : "CA file unchecked",
    bootstrap.stored === true ? "stored" : "",
    bootstrap.sha256_matches === true
      ? "SHA verified"
      : bootstrap.sha256_matches === false
        ? "SHA mismatch"
        : "",
  ].filter(Boolean);
  return parts.length ? parts.join("/") : "CA bootstrap configured";
}
