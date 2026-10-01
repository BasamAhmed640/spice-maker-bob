"""Verify the shipped pixel dissolve and the real Windows setup renderer."""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

Image = pytest.importorskip("PIL.Image")
REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def assets():
    spec = importlib.util.spec_from_file_location(
        "installer_assets", REPO / "installer/render_assets.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_dissolve_leaves_no_pepper_or_trail_then_loops(assets):
    frames = assets.render_splash("Spice Maker", "1.8.2")
    base = assets.splash_base("Spice Maker", "1.8.2")
    assert frames[0].tobytes() != base.tobytes()
    assert frames[40].tobytes() != base.tobytes()
    # A real empty hold follows the last departing square; no permanent outline.
    assert all(frame.tobytes() == base.tobytes() for frame in frames[54:64])
    assert frames[-1].tobytes() == frames[0].tobytes()
    # Title and version never move or fade.
    expected = frames[0].crop((144, 0, 440, 120)).tobytes()
    assert all(frame.crop((144, 0, 440, 120)).tobytes() == expected for frame in frames)


def test_pepper_uses_crisp_binary_square_pixels(assets):
    mark = assets.pixel_mark()
    alpha = mark.getchannel("A")
    assert set(alpha.tobytes()) == {0, 255}
    for y in range(0, assets.SPLASH_H, assets.PIXEL_SIZE):
        for x in range(0, 144, assets.PIXEL_SIZE):
            tile = mark.crop((x, y, x + 4, y + 4))
            assert all(low == high for low, high in tile.getextrema())


def test_gif_retains_holds_fixed_timing_and_decodes_without_ghosts(assets, tmp_path):
    frames = assets.render_splash("Spice Maker", "1.8.2")
    first, second = tmp_path / "first.gif", tmp_path / "second.gif"
    assets.save_gif(frames, first)
    assets.save_gif(frames, second)
    assert first.read_bytes() == second.read_bytes()
    with Image.open(first) as gif:
        assert gif.size == (440, 120)
        assert gif.n_frames == 90
        assert gif.info["loop"] == 0
        assert "transparency" not in gif.info
        decoded = []
        for index in range(gif.n_frames):
            gif.seek(index)
            assert gif.info["duration"] == 40
            assert gif.disposal_method == 1
            decoded.append(gif.convert("RGB").copy())
    # Check decoded/composited GIF, rather than only the unencoded frame source.
    assert decoded[0].tobytes() == decoded[-1].tobytes()
    assert all(frame.tobytes() == decoded[54].tobytes() for frame in decoded[54:64])
    assert decoded[54].getpixel((80, 60)) == assets.SETUP_PAPER


@pytest.mark.skipif(sys.platform != "win32", reason="Windows .NET setup renderer")
def test_actual_setup_renderer_embeds_animates_and_disposes(tmp_path):
    compiler = (
        Path(os.environ.get("WINDIR", "C:/Windows"))
        / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    )
    if not compiler.is_file():
        pytest.skip("Windows Framework C# compiler unavailable")
    harness = tmp_path / "Preview.cs"
    harness.write_text(
        "using System; using System.Reflection; using System.Drawing; "
        "using System.Windows.Forms; using System.Threading;\n"
        "internal static class Preview { [STAThread] static int Main(string[] args) {\n"
        " Application.EnableVisualStyles(); var flags=BindingFlags.Static|BindingFlags.NonPublic;\n"
        ' typeof(PortableInstaller).GetField("Edition", flags).SetValue(null,"Spice Maker");\n'
        ' typeof(PortableInstaller).GetField("Version", flags).SetValue(null,"1.8.2");\n'
        ' using(var form=(Form)typeof(PortableInstaller).GetMethod("CreateSetupForm",flags).Invoke(null,null)) {\n'
        "  form.StartPosition=FormStartPosition.Manual; form.Location=new Point(-30000,-30000);\n"
        "  form.ShowInTaskbar=false; form.Show();\n"
        "  var formHandle=form.Handle; var header=form.Controls[0].Controls[0]; var headerHandle=header.Handle;\n"
        "  var fields=BindingFlags.Instance|BindingFlags.NonPublic;\n"
        '  var image=(Image)header.GetType().GetField("image",fields).GetValue(header);\n'
        "  if(image.GetFrameCount(System.Drawing.Imaging.FrameDimension.Time)!=90) return 2;\n"
        "  var end=DateTime.UtcNow.AddMilliseconds(220);\n"
        "  while(DateTime.UtcNow<end) { Application.DoEvents(); Thread.Sleep(5); }\n"
        '  if((int)header.GetType().GetField("frame",fields).GetValue(header)==0) return 3;\n'
        "  using(var bmp=new Bitmap(form.Width,form.Height)) {\n"
        "   form.DrawToBitmap(bmp,new Rectangle(0,0,form.Width,form.Height)); bmp.Save(args[0]); }\n"
        " } return 0; } }\n",
        encoding="utf-8",
    )
    executable = tmp_path / "Preview.exe"
    subprocess.run(
        [
            str(compiler),
            "/nologo",
            "/target:exe",
            "/main:Preview",
            f"/out:{executable}",
            "/r:System.Windows.Forms.dll",
            "/r:System.Drawing.dll",
            "/r:System.IO.Compression.dll",
            f"/resource:{REPO / 'installer/assets/pepper-splash.gif'},pepper-splash.gif",
            str(REPO / "installer/PortableInstaller.cs"),
            str(harness),
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
    )
    screenshot = tmp_path / "setup-preview.png"
    subprocess.run([str(executable), str(screenshot)], check=True, capture_output=True, timeout=15)
    with Image.open(screenshot) as result:
        assert result.width <= 700 and result.height <= 500
        assert result.height >= 250
        pixels = result.convert("RGB").tobytes()
        assert (
            sum(
                1
                for r, g, b in zip(pixels[0::3], pixels[1::3], pixels[2::3], strict=True)
                if r > 130 and g < 100 and b < 100
            )
            > 200
        )
