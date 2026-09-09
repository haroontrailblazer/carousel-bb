"""The ZIP must contain exactly the selected carousel, with original bytes."""
import copy
from io import BytesIO
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from zipfile import ZipFile

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.state import K_BUNDLE
from web_api import routes_runs
from web_api.auth import Identity
from web_api.deps import current_identity


BUNDLE = {
    'cover': {'video_artifact': 'cover.mp4', 'poster_artifact': 'cover-poster.png'},
    # Intentionally out of order: the export follows slide indices.
    'slides': [{'index': 3, 'artifact': 'slide_03.png'}, {'index': 2, 'artifact': 'slide_02.png'}],
    'cta': {'artifact': 'cta.png'},
}
FILES = {name: (name + '-original-bytes').encode() for name in
         ['cover.mp4', 'cover-poster.png', 'slide_02.png', 'slide_03.png', 'cta.png']}


class CarouselDownloadTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(routes_runs.router, prefix='/api')
        self.app.dependency_overrides[current_identity] = lambda: Identity('reviewer@example.invalid', 'test')
        self.client = TestClient(self.app)
        async def load(**kwargs):
            data = FILES.get(kwargs['filename'])
            return SimpleNamespace(inline_data=SimpleNamespace(data=data))
        self.service = SimpleNamespace(
            latest_versions_async=AsyncMock(return_value={name: 2 for name in FILES}),
            load_artifact=AsyncMock(side_effect=load),
        )
        self.bundle = copy.deepcopy(BUNDLE)
        self.state = patch.object(routes_runs, '_session_state', AsyncMock(return_value={K_BUNDLE: self.bundle}))
        self.artifacts = patch.object(routes_runs.runtime, 'artifact_service', return_value=self.service)
        self.state.start()
        self.artifacts.start()
        self.addCleanup(self.state.stop)
        self.addCleanup(self.artifacts.stop)
        self.addCleanup(self.client.close)

    def test_video_and_image_choices_include_all_slides_and_cta_only_once(self):
        for choice, selected, extension in [('video', 'cover.mp4', 'mp4'), ('image', 'cover-poster.png', 'png')]:
            with self.subTest(choice=choice):
                response = self.client.get(f'/api/runs/run-test/download?cover={choice}')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['content-type'], 'application/zip')
                self.assertIn('attachment;', response.headers['content-disposition'])
                self.assertEqual(response.headers['cache-control'], 'no-store')
                with ZipFile(BytesIO(response.content)) as archive:
                    names = [f'01-cover.{extension}', '02-slide.png', '03-slide.png', '04-cta.png']
                    self.assertEqual(archive.namelist(), names)
                    self.assertEqual([archive.read(name) for name in names],
                                     [FILES[name] for name in [selected, 'slide_02.png', 'slide_03.png', 'cta.png']])
                self.assertEqual(self.bundle, BUNDLE)
        for call in self.service.load_artifact.call_args_list:
            self.assertEqual(call.kwargs['session_id'], 'run-test')
            self.assertEqual(call.kwargs['version'], 2)

    def test_cover_choice_is_required_and_validated(self):
        for query in ['', '?cover=other']:
            self.assertEqual(self.client.get('/api/runs/run-test/download' + query).status_code, 422)
        self.service.load_artifact.assert_not_called()

    def test_requires_signed_in_identity(self):
        self.app.dependency_overrides.clear()
        self.assertEqual(self.client.get('/api/runs/run-test/download?cover=video').status_code, 401)
        self.service.load_artifact.assert_not_called()

    def test_missing_cta_returns_error_instead_of_partial_zip(self):
        self.bundle['cta'] = {}
        response = self.client.get('/api/runs/run-test/download?cover=video')
        self.assertEqual(response.status_code, 409)
        self.service.load_artifact.assert_not_called()

    def test_missing_storage_file_returns_error_instead_of_partial_zip(self):
        self.service.load_artifact.side_effect = lambda **kwargs: None
        response = self.client.get('/api/runs/run-test/download?cover=image')
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()['detail']['code'], 'download_missing_file')

    def test_rejects_cross_namespace_filenames(self):
        self.bundle['cta']['artifact'] = 'user:private.png'
        self.assertEqual(self.client.get('/api/runs/run-test/download?cover=video').status_code, 409)
        self.service.load_artifact.assert_not_called()
