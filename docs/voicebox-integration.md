# Voicebox Integration

Voicebox is a remote service. Increment 2 now includes endpoint records, voice
profiles, per-turn TTS jobs, timing metadata, audio QC, B1 stream generation,
and selective regeneration.

Current implementation:

- Voicebox endpoints are persisted with adapter type, base URL, credential
  reference, timeout, concurrency, retry policy, enabled state, capabilities,
  and health status. The Web UI can create, edit, delete, and health-check
  these records while preserving credential references instead of raw secrets.
- Voice profiles are persisted separately and reference a Voicebox endpoint.
  Default local development seeds a deterministic mock Voicebox endpoint plus
  one voice profile for each default participant. The Web UI can create, edit,
  enable, disable, and delete voice profiles, including language, speaker label,
  model ID, prosody, rate, pitch, and pronunciation dictionary settings.
- `POST /api/v1/voicebox-endpoints/{endpoint_id}/health` checks the deterministic
  mock adapter locally or probes remote `/health` and `/capabilities` endpoints.
  Environment credential references are resolved at request time and raw secrets
  are not stored.
- B1 stream endpoint health uses the stream adapter contract instead of generic
  `/health`: it resolves the configured credential reference, probes the public
  CA bootstrap URL without an Authorization header, records observed and
  expected certificate SHA-256 evidence, and requires the configured
  credential reference to resolve plus the `tls_ca_cert_path` file to exist
  before marking a `b1_voice_stream` endpoint healthy. When
  `generation_canary_enabled=true`, health also posts a short bounded
  `/generate/stream` request with `generation_canary_profile_id` and requires a
  successful RIFF/WAVE response. The recorded canary evidence includes status,
  profile ID, engine, HTTP status, content type, byte count, and text length,
  but never stores bearer credentials or response bodies.
  Live provider readiness exposes the same canary as a compact
  `voice_generation` endpoint summary with a next action such as
  `fix_voicebox_generation_then_rerun_health_check`; it omits the canary text
  and raw provider body.
- `Asset` is now a first-class episode aggregate item with type, language,
  source entity link, storage metadata, duration, checksum, generation metadata,
  and status fields.
- `POST /api/v1/episodes/{episode_id}/audio-assets/plan` selects a target
  transcript, creates one planned audio asset for each non-excluded transcript
  turn, and links each asset to its transcript turn.
- Planned audio assets carry normalized Voicebox generation metadata, including
  transcript version, speaker participant, voice profile, and timing estimate.
- `audio_asset_plan_completeness` QC verifies that every playable transcript
  turn has a planned audio asset.
- `POST /api/v1/episodes/{episode_id}/audio-assets/generate` submits planned
  assets to the selected Voicebox adapter. The deterministic mock adapter marks
  assets completed with real WAV objects in the configured object store,
  checksums, measured durations, job IDs, media probe metadata, and word
  timestamps. Remote JSON adapters send normalized `/tts` requests and persist
  returned storage/timing metadata. B1 stream adapters post the native
  `/generate/stream` payload, receive `audio/wav` bytes synchronously, and store
  the returned WAV in the same object-storage and audio-QC path. The stream
  adapter rejects successful HTTP responses that contain empty audio content or
  a non-audio content type before storage.
  Failed HTTP submissions keep safe diagnostic fields on the audio asset:
  adapter type, Voicebox endpoint ID, voice profile ID, remote profile ID,
  exception type, and failure text. Secrets, request bodies, and provider
  response bodies are not stored.
- B1 native stream responses can provide checksum-bound timing for later
  character lip-sync without changing the `audio/wav` response body. When the
  response includes `x-b1-generation-id`, `x-b1-audio-sha256`, and
  `x-b1-timing-url`, DialectiCore resolves the same-origin timing URL and
  validates that its generation ID and SHA-256 match the received WAV before
  using it. The timing payload supplies CTC forced-aligned word and character
  timestamps plus IPA phonemes placed within those word windows. Persisted
  metadata records `b1_ctc_forced_alignment` for word timing and
  `b1_ipa_from_ctc_word_windows` for the phoneme/viseme track, so consumers do
  not mistake the phoneme spans for acoustic phoneme alignment.
- Timing retrieval is fail-soft: absent, delayed, malformed, cross-origin, or
  checksum-mismatched B1 timing leaves valid audio intact and uses the existing
  provider/text fallback behavior. Endpoint capabilities expose the timing
  lookup path, polling policy, timing method, precision, and phoneme alphabet
  as ordinary UI-editable configuration fields.
- `POST /api/v1/episodes/{episode_id}/speech/produce` is the producer shortcut
  for approved transcripts: it plans missing audio assets and immediately
  generates or submits speech through each character's configured
  `voice_profile_id`.
- Default installations include deterministic mock fallback voices named
  `voice-chatgpt`, `voice-claude`, `voice-deepseek`, `voice-grok`,
  `voice-gemini`, and `voice-mistral`. Frontier-model participant profiles can
  leave `voice_profile_id` blank for B1 assignment and still generate local
  synthetic audio before the remote B1 endpoint is configured because the
  Voicebox service resolves `voice-{participant_id}` when no explicit voice is
  set.
- The Web UI Voicebox panel includes B1 presets that fill a `b1-voicebox`
  endpoint for `https://voice.ai.b1.germering` using
  `adapter_type=b1_voice_stream`, `credential_reference=env:B1_API_KEY`,
  `engine=chatterbox`, and the supplied German native profile UUIDs. Operators
  save those presets as ordinary endpoint/profile records, then can edit,
  disable, or delete them through the normal CRUD controls. Once the
  `b1-voicebox` endpoint exists, the panel can call the backend provisioning
  action to add all missing supplied B1 German voice presets without
  overwriting existing edited profiles. Provisioning assigns saved B1 voices by
  character identity, so `A_ChatGPT` maps to `ChatGPT`, `A_DE_Claude` maps to
  `Claude`, `A_DE_DeepSeek` maps to `DeepSeek`, `A_DE_Grok` maps to `Grok`,
  `A_DE_Gemini` maps to `Gemini`, and `A_DE_Mistral` maps to `Mistral`.
  Existing participant voice choices are preserved; the normal participant
  editor remains the manual override for per-speaker voice selection. The panel
  also exposes a deliberate reassign action for the same `b1-voicebox` endpoint;
  it sends `reassign_participants=true` and moves the matching frontier
  characters back to the native B1 voice presets while leaving unrelated
  participants untouched. The provisioning API writes one safe audit event with
  counts and local IDs only.
- For B1's private Caddy root CA, bootstrap the public certificate before using
  the saved B1 endpoint:

  ```bash
  curl --insecure --fail --silent --show-error \
    --dump-header b1-ca.headers \
    --output b1-ai-hub-caddy-root.crt \
    https://voice.ai.b1.germering/.well-known/b1-ai-hub/caddy-root.crt
  grep -i '^X-B1-SHA256:' b1-ca.headers
  sha256sum b1-ai-hub-caddy-root.crt
  ```

  The expected certificate SHA-256 is
  `326339ee673c53ecca71b7006b177996dbec2c5166b94b2e3cfd56060612bb0c`.
  Keep the bearer token out of source by setting `B1_API_KEY` in the runtime
  environment, by setting `B1_API_KEY_FILE` to an absolute mounted secret file,
  or by changing the endpoint credential reference to a file or Docker-secret
  reference. The production Docker-secret overlay keeps the saved
  `env:B1_API_KEY` preset intact and mounts `./secrets/b1_api_key` at
  `/run/secrets/b1_api_key`; the resolver reads that file when `B1_API_KEY` is
  blank. The Web UI bootstrap action stores the certificate under runtime-state
  certificates, using only the configured
  `tls_ca_cert_path` filename and then updating the saved endpoint to the stored
  runtime-state path. This keeps operator-triggered bootstrap writes inside the
  runtime-state volume; separately, endpoint health checks still verify whatever
  `tls_ca_cert_path` is saved on the endpoint. The B1 preset uses
  `/data/runtime-state/certificates/b1-ai-hub-caddy-root.crt`, which resolves to
  the same runtime-state certificate path in Docker. Endpoint rows summarize B1
  credential and CA status with safe labels such as credential availability,
  file availability, stored bootstrap evidence, and SHA verification without
  rendering the credential reference, resolver error, bootstrap URL, or
  certificate hash. Successful bootstrap also records a dedicated global audit
  event with safe stored/SHA/path readiness flags.
- B1 stream endpoints also record DNS readiness for their configured `base_url`.
  The preset sets `require_base_url_dns_resolution=true`; health becomes
  unhealthy when `voice.ai.b1.germering` cannot resolve from the running
  DialectiCore host, even if a previously bootstrapped CA file is still present.
  Audio generation checks the same flag before submitting a batch and fails fast
  with the endpoint ID, hostname, and resolver error instead of marking every
  per-turn audio asset as a provider failure.
- The B1 presets enable a generation canary using the known Claude test voice:
  remote native endpoints send `engine=chatterbox` and
  `profile_id=bd4e9bf1-482b-4900-97c1-48275d1ba28c`, while the local bridge
  preset uses the matching local profile ID and `engine=remote_http`. Existing
  saved endpoints can disable or adjust this in the endpoint capabilities
  editor, but production readiness should keep a generation canary enabled so
  server-side 500s are visible before episode audio production starts.
- Remote providers that return inline `audio_base64` payloads are written to the
  same configured object store and addressed by stable backend URIs
  (`object://bucket/key` for local storage or `s3://bucket/key` for
  S3-compatible storage).
  Same-origin HTTP(S) result URLs returned as `storage_uri`, `audio_url`,
  `result_url`, `media_url`, or `download_url` are downloaded, stored, and
  rewritten to backend object URIs. External signed URLs require
  `allow_external_result_urls`; `result_download_include_authorization` controls
  whether endpoint bearer credentials are sent to external URLs.
  Stored audio is probed from the actual file or S3 probe cache, using
  `ffprobe` for container metadata, FFmpeg `loudnorm` for integrated loudness
  and normalization-target analysis, and WAV parsing as the fallback.
- Remote adapters may return `submitted` or `running` jobs. `POST
  /api/v1/episodes/{episode_id}/audio-assets/sync` polls the stored
  `remote_job_id` through the endpoint's `job_status_path_template` capability,
  defaulting to `/tts/jobs/{job_id}`, and updates assets when the provider
  returns completed result metadata.
- `POST /api/v1/episodes/{episode_id}/audio-assets/cancel` cancels submitted or
  running remote jobs selected by asset, transcript turn, participant, or target
  language. Remote adapters use `job_cancel_path_template` or
  `cancellation_path_template`, defaulting to `DELETE /tts/jobs/{job_id}`; a
  configured `job_cancel_method` or `cancellation_method` supports providers
  that expose `POST` cancellation endpoints. By default cancelled assets are
  reset to `planned`, preserve `cancelled_remote_job_id`, and are ready for
  selective retry. Already-cancelled assets can be reset again with
  `reset_to_planned: true`, which preserves the previous cancelled job reference
  and makes them eligible for workflow-worker retry.
- Explicit regeneration of submitted or running remote audio assets cancels the
  previous remote job before submitting the replacement, preventing retry
  recovery from orphaning an in-flight provider job when cancellation is
  supported.
- The workflow worker has an `audio` stage after transcript QC/localization. It
  scans approved broadcast or localized transcripts, plans missing per-turn
  audio assets, and calls the same generation path as the API. Synchronous
  providers such as the mock adapter or B1 stream adapter can complete audio in
  this stage; asynchronous providers leave submitted/running jobs for the sync
  stage. Newly created localized transcripts remain `pending_review` and are not
  eligible for this worker until their targeted `localized_transcript_review`
  approval is accepted.
- The `voicebox-adapter` worker now runs the same sync path automatically. It
  scans persisted episodes for submitted or running audio assets, groups them by
  language, calls the remote Voicebox status endpoint, saves updated aggregates,
  and records `audio.jobs.synced` audit events plus generation/media QC.
- `audio_generation_completeness` QC verifies that every playable transcript
  turn has a completed audio asset with storage URI, duration, and checksum.
- Completed generation or sync propagates measured audio duration back to the
  source discussion turns through
  `generation_metadata.actual_audio_duration_seconds_by_language` and updates
  `speaker_balance_state.*.actual_speaking_seconds` for the produced language.
  The latest timing update is recorded in
  `discussion_session.controller_state.actual_audio_timing`.
- `audio_media_integrity` QC is recorded after audio generation and can be rerun
  through `POST /api/v1/episodes/{episode_id}/audio-assets/qc`. It checks the
  available stored-media probe or provider metadata for storage, duration
  plausibility, MIME format, sample rate, channel count, language marker,
  voice-profile consistency, clipping, silence, loudness, true peak, and
  word-timestamp bounds. Stored assets provide FFmpeg integrated loudness,
  loudness range, true peak, target settings, normalization gain/offset, and
  normalization type when FFmpeg is available; stored WAV assets also provide
  waveform-derived peak dBFS, RMS dBFS, silence ratio, and clipping detection.
  External remote URIs that are not explicitly allowed still fall back to
  provider metadata.
- Completed assets include normalized timing tracks for later lip-sync and
  styled subtitle workflows. Provider phonemes are preserved as
  `normalized_phoneme_timestamps`; when only word timings are present, the
  service derives an explicitly estimated phoneme/viseme track. QC validates
  the track and records provider, estimated, ready, and missing timing counts.
- `POST /api/v1/episodes/{episode_id}/audio-assets/generate` supports
  `asset_ids`, `transcript_turn_ids`, `participant_ids`, and `failed_only`
  selections with `regenerate: true`. Selective or explicit regeneration emits
  `audio.assets.regenerated` audit events and increments per-asset generation
  attempt metadata.
- The Web UI can trigger audio planning and generation once the episode is ready
  and displays localized transcript, planned audio, completed audio, audio QC,
  and subtitle QC counts. It also exposes failed-audio retry, manual remote job
  sync, remote job cancellation, and audio QC rerun controls.

Remaining work:

- No known Increment 2 Voicebox implementation gaps remain in the current plan.
- Further Voicebox work should focus on live-provider acceptance runs, provider
  capability expansion, and production hardening discovered from real appliance
  operation.
