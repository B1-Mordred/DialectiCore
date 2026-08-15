from __future__ import annotations

import hashlib
import json
import math
import mimetypes
import shutil
import subprocess
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import quote, unquote

from app.core.config import Settings
from app.services.model_gateway import SecretResolver


@dataclass(frozen=True)
class StoredObject:
    uri: str
    path: Path
    key: str
    backend: str
    checksum: str
    size_bytes: int
    content_type: str


class ObjectStore(Protocol):
    bucket: str

    def put_bytes(
        self,
        key: str,
        payload: bytes,
        content_type: str,
    ) -> StoredObject: ...

    def path_for_uri(self, uri: str) -> Path | None: ...


@dataclass(frozen=True)
class AudioProbeResult:
    duration_ms: int | None
    mime_type: str | None
    sample_rate: int | None
    channels: int | None
    format_name: str | None
    bit_rate: int | None
    size_bytes: int | None
    peak_dbfs: float | None
    rms_dbfs: float | None
    loudness_lufs: float | None
    loudness_source: str | None
    loudness_range_lu: float | None
    loudness_threshold_lufs: float | None
    true_peak_dbtp: float | None
    loudness_target_lufs: float | None
    true_peak_target_dbtp: float | None
    loudness_range_target_lu: float | None
    loudness_normalization_gain_db: float | None
    loudness_target_offset_lu: float | None
    loudness_normalization_type: str | None
    silence_ratio: float | None
    clipping_detected: bool | None
    probe_tool: str
    probe_warnings: list[str]


class LocalObjectStore:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.object_storage_bucket
        self.root = Path(settings.object_storage_local_path).expanduser()

    def put_bytes(
        self,
        key: str,
        payload: bytes,
        content_type: str,
    ) -> StoredObject:
        normalized_key = self._normalize_key(key)
        target = self.root / self.bucket / normalized_key
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
        return StoredObject(
            uri=f"object://{quote(self.bucket)}/{quote(normalized_key)}",
            path=target,
            key=normalized_key,
            backend="local_object_store",
            checksum=checksum,
            size_bytes=len(payload),
            content_type=content_type,
        )

    def path_for_uri(self, uri: str) -> Path | None:
        prefix = f"object://{self.bucket}/"
        if not uri.startswith(prefix):
            return None
        key = uri[len(prefix) :]
        return self.root / self.bucket / self._normalize_key(unquote(key))

    def _normalize_key(self, key: str) -> str:
        normalized = key.strip().lstrip("/")
        parts = [part for part in normalized.split("/") if part and part not in {".", ".."}]
        if not parts:
            raise ValueError("object storage key must not be empty")
        return "/".join(parts)


class S3ObjectStore:
    def __init__(
        self,
        settings: Settings,
        secret_resolver: SecretResolver | None = None,
        client: object | None = None,
    ) -> None:
        self.bucket = settings.object_storage_bucket
        self.endpoint = settings.object_storage_endpoint
        self.region = settings.object_storage_region
        self.force_path_style = settings.object_storage_force_path_style
        self.auto_create_bucket = settings.object_storage_auto_create_bucket
        self.access_key_reference = settings.object_storage_access_key_reference
        self.secret_key_reference = settings.object_storage_secret_key_reference
        self.secret_resolver = secret_resolver or SecretResolver()
        self.root = Path(settings.object_storage_local_path).expanduser()
        self._client = client
        self._bucket_checked = False

    def put_bytes(
        self,
        key: str,
        payload: bytes,
        content_type: str,
    ) -> StoredObject:
        normalized_key = LocalObjectStore._normalize_key(self, key)
        self._ensure_bucket()
        self.client.put_object(
            Bucket=self.bucket,
            Key=normalized_key,
            Body=payload,
            ContentType=content_type,
        )
        cache_path = self._cache_path(normalized_key)
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_bytes(payload)
        checksum = "sha256:" + hashlib.sha256(payload).hexdigest()
        return StoredObject(
            uri=f"s3://{quote(self.bucket)}/{quote(normalized_key)}",
            path=cache_path,
            key=normalized_key,
            backend="s3",
            checksum=checksum,
            size_bytes=len(payload),
            content_type=content_type,
        )

    def path_for_uri(self, uri: str) -> Path | None:
        prefix = f"s3://{self.bucket}/"
        if not uri.startswith(prefix):
            return None
        key = uri[len(prefix) :]
        return self._cache_path(LocalObjectStore._normalize_key(self, unquote(key)))

    @property
    def client(self) -> object:
        if self._client is None:
            self._client = self._build_client()
        return self._client

    def _build_client(self) -> object:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as exc:
            raise RuntimeError(
                "S3 object storage requires the boto3 package to be installed"
            ) from exc

        access_key = self.secret_resolver.resolve(self.access_key_reference)
        secret_key = self.secret_resolver.resolve(self.secret_key_reference)
        if bool(access_key) != bool(secret_key):
            raise RuntimeError(
                "S3 object storage access and secret key references must both resolve"
            )
        kwargs: dict[str, object] = {
            "endpoint_url": self.endpoint,
            "region_name": self.region,
            "config": Config(
                s3={"addressing_style": "path" if self.force_path_style else "auto"}
            ),
        }
        if access_key and secret_key:
            kwargs["aws_access_key_id"] = access_key
            kwargs["aws_secret_access_key"] = secret_key
        return boto3.client("s3", **kwargs)

    def _ensure_bucket(self) -> None:
        if self._bucket_checked or not self.auto_create_bucket:
            return
        try:
            self.client.head_bucket(Bucket=self.bucket)
        except Exception as exc:
            try:
                self.client.create_bucket(Bucket=self.bucket)
            except Exception as create_exc:
                raise RuntimeError(
                    f"S3 object storage bucket {self.bucket!r} is not available"
                ) from create_exc
            if "404" not in str(exc):
                self.client.head_bucket(Bucket=self.bucket)
        self._bucket_checked = True

    def _cache_path(self, normalized_key: str) -> Path:
        return self.root / ".probe-cache" / self.bucket / normalized_key


def create_object_store(
    settings: Settings,
    secret_resolver: SecretResolver | None = None,
) -> ObjectStore:
    backend = settings.object_storage_backend.strip().lower()
    if backend in {"local", "local_object_store", "filesystem"}:
        return LocalObjectStore(settings)
    if backend in {"s3", "s3-compatible", "minio"}:
        return S3ObjectStore(settings, secret_resolver=secret_resolver)
    raise ValueError(f"unsupported object storage backend: {settings.object_storage_backend}")


class AudioProbe:
    def __init__(
        self,
        object_store: ObjectStore,
        target_lufs: float = -16.0,
        true_peak_limit_dbtp: float = -1.5,
        loudness_range_target_lu: float = 11.0,
    ) -> None:
        self.object_store = object_store
        self.target_lufs = target_lufs
        self.true_peak_limit_dbtp = true_peak_limit_dbtp
        self.loudness_range_target_lu = loudness_range_target_lu

    def probe_uri(self, uri: str | None, fallback_mime_type: str | None = None) -> AudioProbeResult:
        if not uri:
            return self._missing_result("missing storage uri")
        path = self.object_store.path_for_uri(uri)
        if path is None:
            return self._missing_result("unsupported storage uri")
        return self.probe_path(path, fallback_mime_type=fallback_mime_type)

    def probe_path(
        self,
        path: Path,
        fallback_mime_type: str | None = None,
    ) -> AudioProbeResult:
        if not path.exists():
            return self._missing_result("stored object not found")
        waveform_metrics = self._probe_wav_signal(path) if path.suffix.lower() == ".wav" else {}
        loudness_metrics = self._probe_loudness(path)
        if ffprobe := shutil.which("ffprobe"):
            result = self._probe_with_ffprobe(path, ffprobe, fallback_mime_type)
            if result.duration_ms is not None:
                return self._merge_probe_metrics(result, waveform_metrics, loudness_metrics)
        if path.suffix.lower() == ".wav":
            result = self._probe_wav(path, fallback_mime_type)
            return self._merge_probe_metrics(result, {}, loudness_metrics)
        result = AudioProbeResult(
            duration_ms=None,
            mime_type=fallback_mime_type or mimetypes.guess_type(path.name)[0],
            sample_rate=None,
            channels=None,
            format_name=None,
            bit_rate=None,
            size_bytes=path.stat().st_size,
            peak_dbfs=None,
            rms_dbfs=None,
            loudness_lufs=None,
            **self._null_loudness_metrics(),
            silence_ratio=None,
            clipping_detected=None,
            probe_tool="filesystem",
            probe_warnings=["no audio probe available for stored object"],
        )
        return self._merge_probe_metrics(result, {}, loudness_metrics)

    def _probe_with_ffprobe(
        self,
        path: Path,
        ffprobe: str,
        fallback_mime_type: str | None,
    ) -> AudioProbeResult:
        command = [
            ffprobe,
            "-v",
            "error",
            "-show_entries",
            "format=duration,format_name,size,bit_rate:stream=codec_type,sample_rate,channels",
            "-of",
            "json",
            str(path),
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            )
            payload = json.loads(completed.stdout or "{}")
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError) as exc:
            return AudioProbeResult(
                duration_ms=None,
                mime_type=fallback_mime_type or mimetypes.guess_type(path.name)[0],
                sample_rate=None,
                channels=None,
                format_name=None,
                bit_rate=None,
                size_bytes=path.stat().st_size if path.exists() else None,
                peak_dbfs=None,
                rms_dbfs=None,
                loudness_lufs=None,
                **self._null_loudness_metrics(),
                silence_ratio=None,
                clipping_detected=None,
                probe_tool="ffprobe",
                probe_warnings=[f"ffprobe failed: {exc}"],
            )
        audio_stream = next(
            (
                stream
                for stream in payload.get("streams", [])
                if stream.get("codec_type") == "audio"
            ),
            {},
        )
        media_format = payload.get("format", {})
        duration = media_format.get("duration")
        return AudioProbeResult(
            duration_ms=int(float(duration) * 1000) if duration is not None else None,
            mime_type=fallback_mime_type or mimetypes.guess_type(path.name)[0],
            sample_rate=self._optional_int(audio_stream.get("sample_rate")),
            channels=self._optional_int(audio_stream.get("channels")),
            format_name=media_format.get("format_name"),
            bit_rate=self._optional_int(media_format.get("bit_rate")),
            size_bytes=self._optional_int(media_format.get("size")),
            peak_dbfs=None,
            rms_dbfs=None,
            loudness_lufs=None,
            **self._null_loudness_metrics(),
            silence_ratio=None,
            clipping_detected=None,
            probe_tool="ffprobe",
            probe_warnings=[],
        )

    def _probe_wav(self, path: Path, fallback_mime_type: str | None) -> AudioProbeResult:
        warnings: list[str] = []
        try:
            with wave.open(str(path), "rb") as audio:
                frames = audio.getnframes()
                sample_rate = audio.getframerate()
                channels = audio.getnchannels()
                duration_ms = int((frames / sample_rate) * 1000) if sample_rate else None
        except (wave.Error, OSError) as exc:
            return AudioProbeResult(
                duration_ms=None,
                mime_type=fallback_mime_type or "audio/wav",
                sample_rate=None,
                channels=None,
                format_name="wav",
                bit_rate=None,
                size_bytes=path.stat().st_size if path.exists() else None,
                peak_dbfs=None,
                rms_dbfs=None,
                loudness_lufs=None,
                **self._null_loudness_metrics(),
                silence_ratio=None,
                clipping_detected=None,
                probe_tool="wave",
                probe_warnings=[f"wave probe failed: {exc}"],
            )
        waveform_metrics = self._probe_wav_signal(path)
        warnings.extend(waveform_metrics.pop("probe_warnings", []))
        return AudioProbeResult(
            duration_ms=duration_ms,
            mime_type=fallback_mime_type or "audio/wav",
            sample_rate=sample_rate,
            channels=channels,
            format_name="wav",
            bit_rate=None,
            size_bytes=path.stat().st_size,
            peak_dbfs=waveform_metrics.get("peak_dbfs"),
            rms_dbfs=waveform_metrics.get("rms_dbfs"),
            loudness_lufs=waveform_metrics.get("loudness_lufs"),
            loudness_source="rms_estimate"
            if waveform_metrics.get("loudness_lufs") is not None
            else None,
            loudness_range_lu=None,
            loudness_threshold_lufs=None,
            true_peak_dbtp=None,
            loudness_target_lufs=None,
            true_peak_target_dbtp=None,
            loudness_range_target_lu=None,
            loudness_normalization_gain_db=None,
            loudness_target_offset_lu=None,
            loudness_normalization_type=None,
            silence_ratio=waveform_metrics.get("silence_ratio"),
            clipping_detected=waveform_metrics.get("clipping_detected"),
            probe_tool="wave",
            probe_warnings=warnings,
        )

    def _probe_wav_signal(self, path: Path) -> dict:
        try:
            with wave.open(str(path), "rb") as audio:
                sample_width = audio.getsampwidth()
                frame_count = audio.getnframes()
                payload = audio.readframes(frame_count)
        except (wave.Error, OSError) as exc:
            return {"probe_warnings": [f"waveform probe failed: {exc}"]}
        if not payload or frame_count <= 0:
            return {"probe_warnings": ["waveform probe found no samples"]}
        if sample_width == 1:
            samples = array("B")
            samples.frombytes(payload)
            values = [int(sample) - 128 for sample in samples]
            max_abs_value = 128
        elif sample_width == 2:
            samples = array("h")
            samples.frombytes(payload)
            values = [int(sample) for sample in samples]
            max_abs_value = 32768
        elif sample_width == 4:
            samples = array("i")
            samples.frombytes(payload)
            values = [int(sample) for sample in samples]
            max_abs_value = 2147483648
        else:
            return {
                "probe_warnings": [
                    f"waveform probe does not support {sample_width}-byte samples"
                ]
            }
        if not values:
            return {"probe_warnings": ["waveform probe found no decoded samples"]}

        peak = max(abs(value) for value in values)
        square_sum = sum(value * value for value in values)
        rms = math.sqrt(square_sum / len(values))
        silence_threshold = max_abs_value * 0.003
        clipping_threshold = max_abs_value * 0.98
        silence_ratio = sum(1 for value in values if abs(value) <= silence_threshold) / len(values)
        peak_dbfs = self._dbfs(peak / max_abs_value)
        rms_dbfs = self._dbfs(rms / max_abs_value)
        return {
            "peak_dbfs": round(peak_dbfs, 2),
            "rms_dbfs": round(rms_dbfs, 2),
            "loudness_lufs": round(rms_dbfs, 2),
            "silence_ratio": round(silence_ratio, 4),
            "clipping_detected": peak >= clipping_threshold,
            "probe_warnings": [],
        }

    def _probe_loudness(self, path: Path) -> dict:
        ffmpeg = shutil.which("ffmpeg")
        if not ffmpeg:
            return {}
        command = [
            ffmpeg,
            "-hide_banner",
            "-nostats",
            "-i",
            str(path),
            "-af",
            (
                "loudnorm="
                f"I={self.target_lufs}:"
                f"TP={self.true_peak_limit_dbtp}:"
                f"LRA={self.loudness_range_target_lu}:"
                "print_format=json"
            ),
            "-f",
            "null",
            "-",
        ]
        try:
            completed = subprocess.run(
                command,
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            payload = self._extract_json_object(completed.stderr)
        except (subprocess.SubprocessError, json.JSONDecodeError, OSError, ValueError) as exc:
            return {
                "probe_warnings": [
                    f"ffmpeg loudness analysis failed: {exc}",
                ]
            }

        input_i = self._optional_float(payload.get("input_i"))
        input_tp = self._optional_float(payload.get("input_tp"))
        input_lra = self._optional_float(payload.get("input_lra"))
        input_thresh = self._optional_float(payload.get("input_thresh"))
        target_offset = self._optional_float(payload.get("target_offset"))
        return {
            "loudness_lufs": input_i,
            "loudness_source": "ffmpeg_loudnorm",
            "loudness_range_lu": input_lra,
            "loudness_threshold_lufs": input_thresh,
            "true_peak_dbtp": input_tp,
            "loudness_target_lufs": self.target_lufs,
            "true_peak_target_dbtp": self.true_peak_limit_dbtp,
            "loudness_range_target_lu": self.loudness_range_target_lu,
            "loudness_normalization_gain_db": round(self.target_lufs - input_i, 2)
            if input_i is not None
            else None,
            "loudness_target_offset_lu": target_offset,
            "loudness_normalization_type": payload.get("normalization_type"),
            "probe_warnings": [],
        }

    def _merge_probe_metrics(
        self,
        result: AudioProbeResult,
        waveform_metrics: dict,
        loudness_metrics: dict,
    ) -> AudioProbeResult:
        warnings = [
            *result.probe_warnings,
            *waveform_metrics.get("probe_warnings", []),
            *loudness_metrics.get("probe_warnings", []),
        ]
        loudness_lufs = self._first_present(
            loudness_metrics.get("loudness_lufs"),
            waveform_metrics.get("loudness_lufs"),
            result.loudness_lufs,
        )
        loudness_source = self._first_present(
            loudness_metrics.get("loudness_source"),
            "rms_estimate" if waveform_metrics.get("loudness_lufs") is not None else None,
            result.loudness_source,
        )
        return AudioProbeResult(
            duration_ms=result.duration_ms,
            mime_type=result.mime_type,
            sample_rate=result.sample_rate,
            channels=result.channels,
            format_name=result.format_name,
            bit_rate=result.bit_rate,
            size_bytes=result.size_bytes,
            peak_dbfs=self._first_present(waveform_metrics.get("peak_dbfs"), result.peak_dbfs),
            rms_dbfs=self._first_present(waveform_metrics.get("rms_dbfs"), result.rms_dbfs),
            loudness_lufs=loudness_lufs,
            loudness_source=loudness_source,
            loudness_range_lu=self._first_present(
                loudness_metrics.get("loudness_range_lu"),
                result.loudness_range_lu,
            ),
            loudness_threshold_lufs=self._first_present(
                loudness_metrics.get("loudness_threshold_lufs"),
                result.loudness_threshold_lufs,
            ),
            true_peak_dbtp=self._first_present(
                loudness_metrics.get("true_peak_dbtp"),
                result.true_peak_dbtp,
            ),
            loudness_target_lufs=self._first_present(
                loudness_metrics.get("loudness_target_lufs"),
                result.loudness_target_lufs,
            ),
            true_peak_target_dbtp=self._first_present(
                loudness_metrics.get("true_peak_target_dbtp"),
                result.true_peak_target_dbtp,
            ),
            loudness_range_target_lu=self._first_present(
                loudness_metrics.get("loudness_range_target_lu"),
                result.loudness_range_target_lu,
            ),
            loudness_normalization_gain_db=self._first_present(
                loudness_metrics.get("loudness_normalization_gain_db"),
                result.loudness_normalization_gain_db,
            ),
            loudness_target_offset_lu=self._first_present(
                loudness_metrics.get("loudness_target_offset_lu"),
                result.loudness_target_offset_lu,
            ),
            loudness_normalization_type=self._first_present(
                loudness_metrics.get("loudness_normalization_type"),
                result.loudness_normalization_type,
            ),
            silence_ratio=self._first_present(
                waveform_metrics.get("silence_ratio"),
                result.silence_ratio,
            ),
            clipping_detected=self._first_present(
                waveform_metrics.get("clipping_detected"),
                result.clipping_detected,
            ),
            probe_tool=result.probe_tool,
            probe_warnings=warnings,
        )

    def _null_loudness_metrics(self) -> dict:
        return {
            "loudness_source": None,
            "loudness_range_lu": None,
            "loudness_threshold_lufs": None,
            "true_peak_dbtp": None,
            "loudness_target_lufs": None,
            "true_peak_target_dbtp": None,
            "loudness_range_target_lu": None,
            "loudness_normalization_gain_db": None,
            "loudness_target_offset_lu": None,
            "loudness_normalization_type": None,
        }

    def _extract_json_object(self, content: str) -> dict:
        start = content.rfind("{")
        end = content.rfind("}")
        if start < 0 or end < start:
            raise ValueError("loudness analysis did not return JSON")
        payload = json.loads(content[start : end + 1])
        if not isinstance(payload, dict):
            raise ValueError("loudness analysis JSON was not an object")
        return payload

    def _first_present(self, *values: object) -> object | None:
        for value in values:
            if value is not None:
                return value
        return None

    def _missing_result(self, warning: str) -> AudioProbeResult:
        return AudioProbeResult(
            duration_ms=None,
            mime_type=None,
            sample_rate=None,
            channels=None,
            format_name=None,
            bit_rate=None,
            size_bytes=None,
            peak_dbfs=None,
            rms_dbfs=None,
            loudness_lufs=None,
            **self._null_loudness_metrics(),
            silence_ratio=None,
            clipping_detected=None,
            probe_tool="none",
            probe_warnings=[warning],
        )

    def _optional_int(self, value: object) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    def _optional_float(self, value: object) -> float | None:
        if value is None:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(parsed):
            return None
        return parsed

    def _dbfs(self, normalized_amplitude: float) -> float:
        if normalized_amplitude <= 0:
            return -120.0
        return 20 * math.log10(normalized_amplitude)
