"""CLI-level tests for `d2z` (convert a disc image to a ZIP archive)."""

import sys
from zipfile import ZipFile

import riscosconv.riscos_zip  # noqa: F401  install the Acorn-extra decode patch
from helpers import build_adf, build_adf_big, build_joliet_iso, ro_load

from riscosconv.cli import cli


def run_d2z(tmp_path, monkeypatch, src_bytes, src_name, *extra):
    """Write a disc image and run `d2z` on it via the CLI."""
    src = tmp_path / src_name
    src.write_bytes(src_bytes)
    out = tmp_path / 'out.zip'
    monkeypatch.setattr(sys, 'argv', ['riscos-conv', 'd2z', str(src), str(out), *extra])
    cli()
    return out


def adf_entries():
    # Old-format ADFS encodes attribute bits in the top bits of each name
    # character, so '!Boot' is stored as \xa1\xc2oot; RiscOsAdfsDisc strips them.
    return [
        (b'\xa1\xc2oot', 0x1000, 0x1000, 4, 100, 0),
        (b'ReadMe', 0x1000, 0x1000, 8, 150, 0),
    ]


def test_d2z_adfs_disc_image(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_adf(adf_entries()), 'disc.adf')
    with ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ['!Boot', 'ReadMe']
        assert zf.read('!Boot') == b'\x00' * 4
        assert zf.read('ReadMe') == b'\x00' * 8


def test_d2z_adfs_disc_image_with_extract_paths(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_adf(adf_entries()), 'disc.adf', 'ReadMe')
    with ZipFile(out) as zf:
        assert zf.namelist() == ['ReadMe']


def test_d2z_adfs_disc_image_extract_paths_no_match(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_adf(adf_entries()), 'disc.adf', 'Nope')
    with ZipFile(out) as zf:
        assert zf.namelist() == []


def test_d2z_adfs_big_fragment_file_round_trips(tmp_path, monkeypatch):
    # F-format disc whose file spans two map zones; the zip must carry the
    # real data read back through the fragment walker.
    out = run_d2z(tmp_path, monkeypatch, build_adf_big(), 'big.adf')
    with ZipFile(out) as zf:
        assert zf.namelist() == ['BIGFILE']
        assert zf.read('BIGFILE') == bytes(i % 256 for i in range(6144))


def test_d2z_accepts_riscos_iso(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_joliet_iso([
        ('AITD/Docs/Cover.jpg', b'img', ro_load(0x003), 1, 3),
        ('AITD/ReadMe.txt', b'readme', ro_load(0x0d4), 2, 3),
        ('Other/Thing.txt', b'other', ro_load(0x003), 1, 3),
    ]), 'disc.iso')
    with ZipFile(out) as zf:
        assert sorted(zf.namelist()) == [
            'AITD/Docs/Cover.jpg', 'AITD/ReadMe.txt', 'Other/Thing.txt']


def test_d2z_iso_with_extract_paths_dir(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_joliet_iso([
        ('AITD/Docs/Cover.jpg', b'img', ro_load(0x003), 1, 3),
        ('AITD/ReadMe.txt', b'readme', ro_load(0x0d4), 2, 3),
        ('Other/Thing.txt', b'other', ro_load(0x003), 1, 3),
    ]), 'disc.iso', 'AITD')
    with ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ['AITD/Docs/Cover.jpg', 'AITD/ReadMe.txt']


def test_d2z_iso_with_extract_paths_subdir(tmp_path, monkeypatch):
    out = run_d2z(tmp_path, monkeypatch, build_joliet_iso([
        ('AITD/Docs/Cover.jpg', b'img', ro_load(0x003), 1, 3),
        ('AITD/ReadMe.txt', b'readme', ro_load(0x0d4), 2, 3),
        ('Other/Thing.txt', b'other', ro_load(0x003), 1, 3),
    ]), 'disc.iso', 'AITD.Docs')
    with ZipFile(out) as zf:
        assert sorted(zf.namelist()) == ['AITD/Docs/Cover.jpg']
