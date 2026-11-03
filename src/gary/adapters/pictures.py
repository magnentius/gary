"""The image model behind /picture (pictures.py holds the words).

Each subject's first picture is kept in the campaign folder under ``pictures/`` and shown again
when asked, so a character keeps one face and the table pays once; ``redraw`` replaces it. New
pictures are capped per day (``GARY_IMAGES_PER_DAY``). Every request sends the style reference
image with the prompt, so the campaign keeps one look.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from .. import pictures

log = logging.getLogger(__name__)

#: Portraits tall, creatures a little wide, places wide.
ASPECT = {"character": "3:4", "npc": "3:4", "creature": "4:3", "place": "16:9"}
SIZE = "1K"  # Discord shows a few hundred pixels and opens larger on click; 2K and 4K would be waste
TIMEOUT_SECONDS = 90.0


class PictureError(RuntimeError):
    """Something to tell the table, as is."""


class Painter:
    def __init__(self, api_key: str, model: str, folder: Path, per_day: int, client: Any = None,
                 describe_model: str = "") -> None:
        self._api_key, self.model, self.folder, self.per_day = api_key, model, Path(folder), int(per_day)
        self._client = client
        self.describe_model = describe_model  # a cheap text model that says what a portrait shows

    @property
    def enabled(self) -> bool:
        return bool((self._api_key or self._client is not None) and self.model and self.per_day > 0)

    def path_for(self, kind: str, name: str) -> Path:
        return self.folder / f"{kind}-{pictures.slug(name)}.jpg"

    def _count_file(self) -> Path:
        return self.folder / "count.json"

    def made_today(self) -> int:
        try:
            counts = json.loads(self._count_file().read_text())
        except (OSError, ValueError):
            return 0
        return int(counts.get(date.today().isoformat(), 0))

    def _count_one(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        self._count_file().write_text(json.dumps({date.today().isoformat(): self.made_today() + 1}))

    async def picture(self, state: Dict[str, Any], kind: str, name: str, redraw: bool = False) -> Tuple[Path, bool]:
        """(the image file, whether it was just made). A redraw sends the kept picture too, so the
        subject stays itself; ``describe`` then says what a new portrait shows."""
        if not self.enabled:
            raise PictureError("Pictures are off: Gary needs a Gemini key, and GARY_IMAGES_PER_DAY above 0.")
        path = self.path_for(kind, name)
        if path.exists() and not redraw:
            return path, False
        if self.made_today() >= self.per_day:
            raise PictureError(f"That's today's {self.per_day} new pictures. Kept ones can still be shown; more "
                               "tomorrow.")
        before = path.read_bytes() if path.exists() else None
        text = pictures.prompt(state, kind, name)
        if before is not None:
            text += " " + pictures.SAME_SUBJECT.format(kind="person" if pictures.is_person(kind) else kind)
        data = await self._draw(text, ASPECT.get(kind, "1:1"), before)
        self.folder.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self._count_one()
        return path, True

    def _client_now(self) -> Any:
        if self._client is None:
            from google import genai

            self._client = genai.Client(api_key=self._api_key)
        return self._client

    async def describe(self, path: Path) -> str:
        """What a portrait shows, in a line, so the narration and later pictures match it. "" if
        there is no model for it or it fails -- the picture stands without it."""
        if not self.describe_model:
            return ""
        from google.genai import types

        try:
            response = await asyncio.wait_for(self._client_now().aio.models.generate_content(
                model=self.describe_model,
                contents=[types.Part.from_bytes(data=path.read_bytes(), mime_type="image/jpeg"), pictures.DESCRIBE]),
                timeout=TIMEOUT_SECONDS)
        except Exception:  # noqa: BLE001
            log.exception("could not describe a portrait")
            return ""
        return " ".join(str(getattr(response, "text", "") or "").split())[:240]

    async def _draw(self, text: str, aspect: str, before: Optional[bytes] = None) -> bytes:
        from google.genai import types

        self._client_now()
        reference = types.Part.from_bytes(data=pictures.STYLE_REFERENCE.read_bytes(), mime_type="image/jpeg")
        contents = [reference] + ([types.Part.from_bytes(data=before, mime_type="image/jpeg")] if before else [])
        config = types.GenerateContentConfig(
            response_modalities=["IMAGE"],
            image_config=types.ImageConfig(aspect_ratio=aspect, image_size=SIZE))
        try:
            response = await asyncio.wait_for(self._client.aio.models.generate_content(
                model=self.model, contents=[*contents, text], config=config), timeout=TIMEOUT_SECONDS)
        except asyncio.TimeoutError as exc:
            raise PictureError("The image model took too long. Try again in a moment.") from exc
        except Exception as exc:  # noqa: BLE001 - the table hears a sentence, the log the detail
            log.exception("image generation failed")
            raise PictureError("The image model couldn't draw that just now. Try again in a moment.") from exc
        found = _image_of(response)
        if found is None:
            raise PictureError("The image model declined to draw that -- it may have found it too violent or "
                               "too close to a real person. Try `redraw`, or another subject.")
        return found


def _image_of(response: Any) -> Optional[bytes]:
    for candidate in getattr(response, "candidates", None) or ():
        content = getattr(candidate, "content", None)
        for part in getattr(content, "parts", None) or ():
            inline = getattr(part, "inline_data", None)
            if inline is not None and getattr(inline, "data", None):
                return inline.data
    return None
