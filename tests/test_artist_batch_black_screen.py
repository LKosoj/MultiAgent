import os
import tempfile

from PIL import Image

from custom_tools.storybook.artist_batch_edit import _write_solid_color_png


def test_write_solid_color_png_is_pure_black():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "black.png")
        out = _write_solid_color_png(path, 8, 8, (0, 0, 0))
        assert os.path.isfile(out)
        im = Image.open(out).convert("RGB")
        assert im.size == (8, 8)
        assert im.getpixel((3, 3)) == (0, 0, 0)
