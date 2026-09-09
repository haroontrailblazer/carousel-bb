"""Bad cover copy should be repairable by the agent, not terminate its run."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from PIL import Image

from app.agents import first_page_visual


class CoverToolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_title_overflow_returns_retryable_tool_result(self):
        context = SimpleNamespace(state={})
        with tempfile.TemporaryDirectory() as tmp:
            media = Path(tmp) / 'existing-source.png'
            Image.new('RGB', (1080, 1350)).save(media)
            with patch.object(first_page_visual, '_run_workdir', return_value=Path(tmp)):
                result = await first_page_visual.build_cover(
                    str(media), False, title='W' * 80,
                    tool_context=context,
                )
        self.assertFalse(result['ok'])
        self.assertIn('Shorten the title', result['error'])
        self.assertIn('reuse the same media_path', result['error'])
        self.assertNotIn('cover', context.state)
