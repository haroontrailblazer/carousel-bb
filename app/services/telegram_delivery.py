"""Deliver every finished asset and the caption without requesting a verdict."""

import asyncio
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from zipfile import ZIP_STORED, ZipFile
from google.adk.tools import ToolContext

from app.schemas import Bundle
from app.state import K_BUNDLE, K_RUN_ID, K_TELEGRAM_DELIVERY, get_model
from app.tools.telegram_tools import send_completed_carousel


async def deliver(tool_context: ToolContext) -> dict:
    """Build an archive from stored artifacts and return Telegram's receipt."""
    bundle = get_model(tool_context.state, K_BUNDLE, Bundle)
    if not bundle or not bundle.ordered_artifacts:
        raise ValueError("The completed carousel has no assets to send.")
    filenames = list(dict.fromkeys([
        *bundle.ordered_artifacts,
        bundle.cover.poster_artifact, bundle.cover.video_artifact,
    ]))
    with SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b") as archive:
        with ZipFile(archive, "w", compression=ZIP_STORED) as zip_file:
            for index, filename in enumerate(filter(None, filenames), 1):
                part = await tool_context.load_artifact(filename)
                data = part.inline_data.data if part and part.inline_data else None
                if not data:
                    raise ValueError(f"Carousel asset is unavailable: {filename}")
                name = f"{index:02d}-{PurePosixPath(filename).name}"
                await asyncio.to_thread(zip_file.writestr, name, data)
            zip_file.writestr("caption.txt", bundle.caption)
        if archive.tell() > 50 * 1024 * 1024:
            raise ValueError("Carousel archive exceeds Telegram's 50 MB upload limit.")
        archive.seek(0)
        result = await asyncio.to_thread(
            send_completed_carousel, str(tool_context.state.get(K_RUN_ID) or ""),
            archive, bundle.cover.title,
            previous=tool_context.state.get(K_TELEGRAM_DELIVERY),
        )
    return {"status": "delivered", **result}
