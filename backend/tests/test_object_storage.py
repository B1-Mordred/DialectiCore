import io
import struct
import subprocess
import wave
from pathlib import Path

import pytest
from app.core.config import Settings
from app.services.object_storage import (
    AudioProbe,
    LocalObjectStore,
    S3ObjectStore,
    create_object_store,
)


class FakeS3Client:
    def __init__(self) -> None:
        self.bucket_exists = False
        self.created_buckets: list[str] = []
        self.puts: list[dict] = []

    def head_bucket(self, Bucket: str) -> None:
        if not self.bucket_exists:
            raise RuntimeError("404 bucket not found")

    def create_bucket(self, Bucket: str) -> None:
        self.bucket_exists = True
        self.created_buckets.append(Bucket)

    def put_object(self, **kwargs: object) -> None:
        self.puts.append(kwargs)


def test_local_object_store_writes_object_uri(tmp_path: Path) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "objects"))
    store = LocalObjectStore(settings)

    stored = store.put_bytes("audio/test clip.wav", b"payload", "audio/wav")

    assert stored.backend == "local_object_store"
    assert stored.key == "audio/test clip.wav"
    assert stored.uri == "object://dialecticore/audio/test%20clip.wav"
    assert stored.path.read_bytes() == b"payload"
    assert store.path_for_uri(stored.uri) == stored.path


def test_s3_object_store_puts_object_and_writes_probe_cache(tmp_path: Path) -> None:
    client = FakeS3Client()
    store = S3ObjectStore(
        Settings(
            object_storage_backend="s3",
            object_storage_local_path=str(tmp_path / "objects"),
        ),
        client=client,
    )
    payload = wav_bytes([0, 6000, -6000, 0] * 12000)

    stored = store.put_bytes("audio/episode one/turn.wav", payload, "audio/wav")
    probe = AudioProbe(store).probe_uri(stored.uri, fallback_mime_type="audio/wav")

    assert client.created_buckets == ["dialecticore"]
    assert client.puts == [
        {
            "Bucket": "dialecticore",
            "Key": "audio/episode one/turn.wav",
            "Body": payload,
            "ContentType": "audio/wav",
        }
    ]
    assert stored.backend == "s3"
    assert stored.key == "audio/episode one/turn.wav"
    assert stored.uri == "s3://dialecticore/audio/episode%20one/turn.wav"
    assert stored.path.read_bytes() == payload
    assert store.path_for_uri(stored.uri) == stored.path
    assert probe.duration_ms == 1000
    assert probe.size_bytes == len(payload)
    assert probe.peak_dbfs is not None


def test_audio_probe_records_ffmpeg_loudness_normalization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(object_storage_local_path=str(tmp_path / "objects"))
    store = LocalObjectStore(settings)
    stored = store.put_bytes(
        "audio/loudness.wav",
        wav_bytes([0, 6000, -6000, 0] * 12000),
        "audio/wav",
    )

    def fake_which(binary: str) -> str | None:
        return "/usr/bin/ffmpeg" if binary == "ffmpeg" else None

    def fake_run(
        command: list[str],
        check: bool,
        capture_output: bool,
        text: bool,
        timeout: int,
    ) -> subprocess.CompletedProcess:
        assert check is True
        assert capture_output is True
        assert text is True
        assert timeout == 20
        assert command[0] == "/usr/bin/ffmpeg"
        assert "loudnorm=I=-16.0:TP=-1.5:LRA=11.0:print_format=json" in command
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="",
            stderr=(
                "frame output\n"
                "{\n"
                '  "input_i" : "-23.00",\n'
                '  "input_tp" : "-4.50",\n'
                '  "input_lra" : "3.20",\n'
                '  "input_thresh" : "-33.10",\n'
                '  "normalization_type" : "dynamic",\n'
                '  "target_offset" : "-0.25"\n'
                "}\n"
            ),
        )

    monkeypatch.setattr("app.services.object_storage.shutil.which", fake_which)
    monkeypatch.setattr("app.services.object_storage.subprocess.run", fake_run)

    probe = AudioProbe(store).probe_uri(stored.uri, fallback_mime_type="audio/wav")

    assert probe.loudness_lufs == -23.0
    assert probe.loudness_source == "ffmpeg_loudnorm"
    assert probe.loudness_range_lu == 3.2
    assert probe.loudness_threshold_lufs == -33.1
    assert probe.true_peak_dbtp == -4.5
    assert probe.loudness_target_lufs == -16.0
    assert probe.true_peak_target_dbtp == -1.5
    assert probe.loudness_range_target_lu == 11.0
    assert probe.loudness_normalization_gain_db == 7.0
    assert probe.loudness_target_offset_lu == -0.25
    assert probe.loudness_normalization_type == "dynamic"
    assert probe.probe_warnings == []


def test_create_object_store_selects_configured_backend(tmp_path: Path) -> None:
    local = create_object_store(
        Settings(
            object_storage_backend="filesystem",
            object_storage_local_path=str(tmp_path / "local"),
        )
    )
    s3 = create_object_store(
        Settings(
            object_storage_backend="minio",
            object_storage_local_path=str(tmp_path / "s3"),
        )
    )

    assert isinstance(local, LocalObjectStore)
    assert isinstance(s3, S3ObjectStore)


def test_create_object_store_rejects_unknown_backend() -> None:
    with pytest.raises(ValueError, match="unsupported object storage backend"):
        create_object_store(Settings(object_storage_backend="tape"))


def wav_bytes(samples: list[int], sample_rate: int = 48000) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setnchannels(1)
        audio.setsampwidth(2)
        audio.setframerate(sample_rate)
        audio.writeframes(b"".join(struct.pack("<h", sample) for sample in samples))
    return buffer.getvalue()
