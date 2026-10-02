"""Guard the zip_implode monkey-patch against being silently dropped.

`riscos_zip` imports `zip_implode` purely for its side effect -- importing it
runs `setup_zip_implode()`, which monkey-patches `zipfile` with a decompressor
for the Implode method (ZIP method 6). Because the imported name is never
referenced, a linter or a well-meaning import cleanup can delete that line and
nothing else in the suite notices: no other test builds an Implode-compressed
archive.

The guard therefore has to run in a *fresh interpreter* that imports nothing
but `riscos_zip`. Asserting on `zipfile` in-process would be useless, since any
test that imports `zip_implode` for its symbols installs the patch itself and
would mask the regression.
"""

import os
import subprocess
import sys
import zipfile

import riscosconv.riscos_zip  # noqa: F401
from riscosconv.zip_implode import ZIP_IMPLODE, _ImplodeDecompressor


def test_importing_riscos_zip_installs_the_patch():
    code = (
        "import zipfile\n"
        "import riscosconv.riscos_zip\n"
        "assert hasattr(zipfile, 'ZIP_IMPLODE'), (\n"
        "    'importing riscosconv.riscos_zip did not install the implode patch'\n"
        ")\n"
        "zipfile._check_compression(zipfile.ZIP_IMPLODE)\n"
    )
    env = dict(os.environ)
    # Let the child import riscosconv the same way this interpreter did
    # (via pytest's `pythonpath` setting, or an editable install).
    env["PYTHONPATH"] = os.pathsep.join(p for p in sys.path if p)

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0, result.stderr


def test_get_decompressor_returns_implode_decompressor():
    assert isinstance(zipfile._get_decompressor(ZIP_IMPLODE), _ImplodeDecompressor)


def test_patch_wraps_rather_than_replaces_originals():
    # Deflate must keep working -- the patch chains to the originals.
    assert zipfile._get_decompressor(zipfile.ZIP_DEFLATED) is not None
