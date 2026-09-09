"""Export the reviewed carousel without changing its publishing state."""
import asyncio
from pathlib import PurePosixPath
from tempfile import SpooledTemporaryFile
from typing import BinaryIO, Literal
from zipfile import ZIP_STORED, ZipFile

from fastapi import HTTPException


def download_entries(bundle: dict, cover_choice: Literal['video', 'image']) -> list[tuple[str, str]]:
    """Select one cover and name every image in carousel order."""
    cover = bundle.get('cover') or {}
    selected = cover.get('video_artifact' if cover_choice == 'video' else 'poster_artifact')
    slides = sorted(bundle.get('slides') or [], key=lambda slide: slide['index'])
    cta = (bundle.get('cta') or {}).get('artifact')
    if not selected or not slides or not cta:
        raise HTTPException(409, {'code': 'download_not_ready', 'message': 'The selected cover, slides and CTA must finish rendering before downloading.'})
    files = [(selected, 'cover')]
    files.extend((slide.get('artifact'), 'slide') for slide in slides)
    files.append((cta, 'cta'))
    entries = []
    for index, (filename, label) in enumerate(files, 1):
        if not filename or PurePosixPath(filename).name != filename or ':' in filename or '\\' in filename:
            raise HTTPException(409, {'code': 'download_not_ready', 'message': 'A carousel file is missing or invalid. Refresh the review and try again.'})
        extension = PurePosixPath(filename).suffix.lower()
        if extension not in {'.png', '.jpg', '.jpeg', '.webp', '.mp4'}:
            raise HTTPException(409, {'code': 'download_not_ready', 'message': 'A carousel file has an unsupported format.'})
        entries.append((filename, f'{index:02d}-{label}{extension}'))
    return entries


async def build_archive(service, *, bundle: dict, cover_choice: Literal['video', 'image'],
                        app_name: str, user_id: str, run_id: str) -> BinaryIO:
    entries = download_entries(bundle, cover_choice)
    versions = await service.latest_versions_async(app_name, user_id, run_id)
    archive = SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode='w+b')
    try:
        with ZipFile(archive, 'w', compression=ZIP_STORED) as zip_file:
            for filename, download_name in entries:
                if filename not in versions:
                    raise HTTPException(409, {'code': 'download_missing_file', 'message': 'A carousel file is unavailable. Refresh the review and try again.'})
                part = await service.load_artifact(
                    app_name=app_name, user_id=user_id, session_id=run_id,
                    filename=filename, version=versions[filename],
                )
                data = part.inline_data.data if part and part.inline_data else None
                if not data:
                    raise HTTPException(409, {'code': 'download_missing_file', 'message': 'A carousel file is unavailable. Refresh the review and try again.'})
                await asyncio.to_thread(zip_file.writestr, download_name, data)
        archive.seek(0)
        return archive
    except BaseException:
        archive.close()
        raise
