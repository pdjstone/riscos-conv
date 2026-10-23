"""Check the built wheel's version against setuptools-scm, and against the
tag when given one. Used by CI and the publish workflow.

Usage: python scripts/check_version.py [TAG]
"""

import glob
import subprocess
import sys
import zipfile


def wheel_version():
    wheels = glob.glob("dist/*.whl")
    if len(wheels) != 1:
        sys.exit(f"expected exactly one wheel in dist/, found {wheels}")
    with zipfile.ZipFile(wheels[0]) as z:
        metadata = next(
            n for n in z.namelist() if n.endswith(".dist-info/METADATA")
        )
        for line in z.read(metadata).decode().splitlines():
            if line.startswith("Version: "):
                return line.split(": ", 1)[1]
    sys.exit(f"no Version in {wheels[0]} metadata")


def main():
    version = wheel_version()
    scm_version = subprocess.run(
        [sys.executable, "-m", "setuptools_scm"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if version != scm_version:
        sys.exit(f"wheel version {version!r} != setuptools-scm {scm_version!r}")
    print(f"wheel version {version} matches setuptools-scm")

    # On a release the version must be exactly the tag: a .dev or +g<sha>
    # suffix means the tag isn't on the commit that was built.
    if len(sys.argv) > 1:
        tag = sys.argv[1]
        if version != tag.removeprefix("v"):
            sys.exit(f"tag {tag!r} but wheel version is {version!r}")
        print(f"matches tag {tag}")


if __name__ == "__main__":
    main()
