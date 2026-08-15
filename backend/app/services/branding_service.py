from __future__ import annotations

import hashlib
import io
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

from app.core.config import Settings
from app.domain.enums import AssetType
from app.domain.schemas import Asset, BrandLogoMetadata, Episode, Project
from app.services.object_storage import ObjectStore, create_object_store
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

MAX_LOGO_BYTES = 10 * 1024 * 1024
MAX_LOGO_PIXELS = 40_000_000
SUPPORTED_LOGO_FORMATS = frozenset({"PNG", "JPEG", "WEBP"})
BUNDLED_LOGO_REVISION = "dialecticore-mark-v1"


class BrandingService:
    """Creates checksum-bound brand images without mutating prior revisions."""

    def __init__(
        self,
        settings: Settings,
        object_store: ObjectStore | None = None,
    ) -> None:
        self.settings = settings
        self.object_store = object_store or create_object_store(settings)

    def store_logo(
        self,
        payload: bytes,
        *,
        source: str,
        owner_id: str,
    ) -> BrandLogoMetadata:
        if not payload:
            raise ValueError("logo upload is empty")
        if len(payload) > MAX_LOGO_BYTES:
            raise ValueError("logo upload exceeds the 10 MiB limit")
        normalized, width, height = self._normalize_logo(payload)
        digest = hashlib.sha256(normalized).hexdigest()
        revision_id = f"{source}-{digest[:16]}"
        stored = self.object_store.put_bytes(
            key=f"branding/{source}/{owner_id}/{revision_id}.png",
            payload=normalized,
            content_type="image/png",
        )
        return BrandLogoMetadata(
            revision_id=revision_id,
            storage_uri=stored.uri,
            checksum=stored.checksum,
            width=width,
            height=height,
            mime_type="image/png",
            size_bytes=stored.size_bytes,
            source=source,
            uploaded_at=datetime.now(UTC),
        )

    def bundled_logo(self) -> BrandLogoMetadata:
        payload = self._bundled_logo_png()
        digest = hashlib.sha256(payload).hexdigest()
        stored = self.object_store.put_bytes(
            key=f"branding/bundled/{BUNDLED_LOGO_REVISION}-{digest[:16]}.png",
            payload=payload,
            content_type="image/png",
        )
        return BrandLogoMetadata(
            revision_id=BUNDLED_LOGO_REVISION,
            storage_uri=stored.uri,
            checksum=stored.checksum,
            width=1200,
            height=400,
            mime_type="image/png",
            size_bytes=stored.size_bytes,
            source="bundled",
        )

    def effective_branding(
        self,
        episode: Episode,
        project: Project | None,
    ) -> tuple[str, BrandLogoMetadata]:
        show_name = (
            project.branding.show_name
            if project is not None and project.branding.show_name
            else project.name
            if project is not None
            else "DialectiCore"
        )
        logo = episode.definition.media.branding.logo_override
        if logo is None and project is not None:
            logo = project.branding.logo
        return show_name, logo or self.bundled_logo()

    def ensure_identity_slate(
        self,
        episode: Episode,
        project: Project | None,
    ) -> Asset:
        show_name, logo = self.effective_branding(episode, project)
        derivation = {
            "schema_version": "show_identity_slate.v2",
            "show_name": show_name,
            "episode_title": episode.title,
            "logo_checksum": logo.checksum,
            "width": episode.definition.media.width,
            "height": episode.definition.media.height,
        }
        derivation_hash = hashlib.sha256(
            repr(sorted(derivation.items())).encode("utf-8")
        ).hexdigest()
        existing = next(
            (
                asset
                for asset in reversed(episode.assets)
                if asset.asset_type == AssetType.image
                and asset.status == "completed"
                and asset.generation_metadata.get("visual_role") == "show_identity_slate"
                and asset.generation_metadata.get("derivation_hash") == derivation_hash
            ),
            None,
        )
        if existing is not None:
            return existing

        logo_path = self.object_store.path_for_uri(logo.storage_uri)
        if logo_path is None or not logo_path.exists():
            raise ValueError("effective show logo is unavailable in object storage")
        payload, layout = self._identity_slate_png(
            logo_path=logo_path,
            title=episode.title,
            show_name=show_name,
            width=episode.definition.media.width,
            height=episode.definition.media.height,
        )
        asset_id = uuid4()
        stored = self.object_store.put_bytes(
            key=f"branding/episodes/{episode.id}/identity-slates/{asset_id}.png",
            payload=payload,
            content_type="image/png",
        )
        asset = Asset(
            id=asset_id,
            episode_id=episode.id,
            asset_type=AssetType.image,
            language=episode.source_language,
            source_entity_type="episode_branding",
            source_entity_id=str(episode.id),
            storage_uri=stored.uri,
            mime_type="image/png",
            width=episode.definition.media.width,
            height=episode.definition.media.height,
            checksum=stored.checksum,
            status="completed",
            generation_metadata={
                **derivation,
                "derivation_hash": derivation_hash,
                "visual_role": "show_identity_slate",
                "logo": logo.model_dump(mode="json"),
                "layout": layout,
                "object_storage_path": str(stored.path),
                "storage_backend": stored.backend,
            },
        )
        episode.assets.append(asset)
        return asset

    @staticmethod
    def _normalize_logo(payload: bytes) -> tuple[bytes, int, int]:
        try:
            with Image.open(io.BytesIO(payload)) as source_image:
                if source_image.format not in SUPPORTED_LOGO_FORMATS:
                    raise ValueError("logo must be PNG, JPEG, or WebP")
                width, height = source_image.size
                if width < 1 or height < 1 or width * height > MAX_LOGO_PIXELS:
                    raise ValueError("logo pixel dimensions are outside the safe limit")
                image = source_image.convert("RGBA")
        except (UnidentifiedImageError, OSError) as exc:
            raise ValueError("logo is not a valid PNG, JPEG, or WebP image") from exc
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True, compress_level=9)
        return output.getvalue(), width, height

    @staticmethod
    def _font(size: int, *, bold: bool = False) -> ImageFont.FreeTypeFont:
        filename = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
        candidates = (
            Path("/usr/share/fonts/truetype/dejavu") / filename,
            Path("/usr/share/fonts/dejavu") / filename,
        )
        for path in candidates:
            if path.exists():
                return ImageFont.truetype(str(path), size=size)
        raise ValueError("the deterministic DejaVu font required for branding is unavailable")

    def _bundled_logo_png(self) -> bytes:
        image = Image.new("RGBA", (1200, 400), (0, 0, 0, 0))
        draw = ImageDraw.Draw(image)
        accent = (220, 38, 38, 255)
        white = (248, 250, 252, 255)
        center = (175, 200)
        for radius, stroke in ((104, 16), (72, 14), (38, 12)):
            draw.ellipse(
                (center[0] - radius, center[1] - radius, center[0] + radius, center[1] + radius),
                outline=accent,
                width=stroke,
            )
        draw.ellipse((157, 182, 193, 218), fill=white)
        font = self._font(142, bold=True)
        draw.text((330, 115), "DialectiCore", font=font, fill=white)
        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True, compress_level=9)
        return output.getvalue()

    def _identity_slate_png(
        self,
        *,
        logo_path: Path,
        title: str,
        show_name: str,
        width: int,
        height: int,
    ) -> tuple[bytes, dict[str, object]]:
        image = Image.new("RGBA", (width, height), (8, 15, 28, 255))
        draw = ImageDraw.Draw(image)
        margin_x = max(48, int(width * 0.08))
        safe_width = width - 2 * margin_x
        with Image.open(logo_path) as logo_source:
            logo = logo_source.convert("RGBA")
        # The slate is designed for the visible upper band of a studio rear
        # screen. Seated characters legitimately occlude its lower edge, so
        # identity and title pixels stay above that normal foreground region.
        logo_max_w = int(safe_width * 0.45)
        logo_max_h = int(height * 0.13)
        scale = min(logo_max_w / logo.width, logo_max_h / logo.height, 1.0)
        logo_size = (max(1, round(logo.width * scale)), max(1, round(logo.height * scale)))
        logo = logo.resize(logo_size, Image.Resampling.LANCZOS)
        logo_x = (width - logo.width) // 2
        logo_y = int(height * 0.20)
        image.alpha_composite(logo, (logo_x, logo_y))

        divider_y = logo_y + logo.height + max(10, int(height * 0.015))
        draw.rounded_rectangle(
            (int(width * 0.35), divider_y, int(width * 0.65), divider_y + 5),
            radius=2,
            fill=(220, 38, 38, 255),
        )
        title_top = divider_y + max(14, int(height * 0.02))
        title_bottom = int(height * 0.58)
        chosen: tuple[ImageFont.FreeTypeFont, list[str], int, int] | None = None
        minimum_font_size = max(24, int(height * 0.036))
        for font_size in range(
            max(minimum_font_size, int(height * 0.058)), minimum_font_size - 1, -2
        ):
            font = self._font(font_size, bold=True)
            lines = self._wrap_title(draw, title, font, safe_width)
            line_height = int(font_size * 1.22)
            block_height = line_height * len(lines)
            if len(lines) <= 4 and block_height <= title_bottom - title_top:
                chosen = (font, lines, line_height, block_height)
                break
        if chosen is None:
            raise ValueError("episode title cannot be rendered legibly on the identity slate")
        font, lines, line_height, block_height = chosen
        text_y = title_top
        line_boxes: list[dict[str, int | str]] = []
        for line in lines:
            box = draw.textbbox((0, 0), line, font=font)
            text_width = box[2] - box[0]
            text_x = (width - text_width) // 2
            draw.text((text_x, text_y), line, font=font, fill=(248, 250, 252, 255))
            line_boxes.append(
                {"text": line, "x": text_x, "y": text_y, "width": text_width, "height": line_height}
            )
            text_y += line_height

        output = io.BytesIO()
        image.save(output, format="PNG", optimize=True, compress_level=9)
        return output.getvalue(), {
            "schema_version": "show_identity_slate_layout.v2",
            "show_name": show_name,
            "title_exact": title,
            "title_line_count": len(lines),
            "title_font_size": font.size,
            "title_lines": line_boxes,
            "logo_box": {"x": logo_x, "y": logo_y, "width": logo.width, "height": logo.height},
            "safe_margin_x": margin_x,
            "title_truncated": False,
        }

    @staticmethod
    def _wrap_title(
        draw: ImageDraw.ImageDraw,
        title: str,
        font: ImageFont.FreeTypeFont,
        max_width: int,
    ) -> list[str]:
        words = title.split()
        if not words:
            raise ValueError("episode title must not be empty")
        lines: list[str] = []
        current = words[0]
        for word in words[1:]:
            candidate = f"{current} {word}"
            if draw.textlength(candidate, font=font) <= max_width:
                current = candidate
                continue
            if draw.textlength(word, font=font) > max_width:
                raise ValueError("episode title contains a word too long for the identity slate")
            lines.append(current)
            current = word
        lines.append(current)
        return lines
