from datetime import datetime
from io import BytesIO
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from riscosconv.arcfs import ArcFSArchive
from riscosconv.ro_file_meta import RiscOsFileMeta
from riscosconv.riscos_path import PureRiscOsPath
from riscosconv.riscos_zip import RiscOsZip, convert_disc_to_zip, zip_extra

from arcfs_builder import ArcFSFile, build_arcfs


def make_zip(entries, *, acorn_extra=True):
    """Build a zip in memory; entries are (name, data) or (name, data, ro_meta)"""
    buf = BytesIO()
    with ZipFile(buf, 'w') as zf:
        for entry in entries:
            name, data, *rest = entry
            info = ZipInfo(name, (1998, 6, 14, 17, 0, 52))
            info.compress_type = ZIP_DEFLATED
            if acorn_extra and rest:
                info.extra = zip_extra(rest[0])
            zf.writestr(info, data)
    buf.seek(0)
    return buf


def test_zipname_with_colon_substituted():
    # ':' can't appear in a RISC OS name (filing-system prefix); one foreign
    # entry must not make the whole archive unopenable
    ro_meta = RiscOsFileMeta.from_datestamp(0)
    buf = make_zip([('notes:backup.txt', b'x', ro_meta)])
    archive = RiscOsZip(buf)
    [(path, meta)] = list(archive.list())
    assert path == PureRiscOsPath('notes_backup.txt')
    with archive.open(path) as f:
        assert f.read() == b'x'


def test_dotted_leafname_entry():
    # '.' is the RISC OS separator, so a dotted leafname parses as components;
    # entry must still list and open via the same parsed path
    ro_meta = RiscOsFileMeta.from_datestamp(0)
    buf = make_zip([('!Horizon/music.s3m', b'tune', ro_meta)])
    archive = RiscOsZip(buf)
    [(path, _)] = list(archive.list())
    assert path == PureRiscOsPath('!Horizon.music.s3m')
    with archive.open('!Horizon.music.s3m') as f:
        assert f.read() == b'tune'


def test_missing_acorn_extra_field():
    # foreign zipper: no Acorn extra field -> fallback text type, zip timestamp
    buf = make_zip([('!BadApple/vectout15', b'v')], acorn_extra=False)
    archive = RiscOsZip(buf)
    [(path, meta)] = list(archive.list())
    assert path == PureRiscOsPath('!BadApple.vectout15')
    assert meta.ro_meta.filetype == 0xfff
    assert meta.timestamp == datetime(1998, 6, 14, 17, 0, 52)


def test_convert_filter_matches_component_boundaries(tmp_path):
    # filter 'Draw' must not pull in 'DrawDemo', and must include 'Draw.x'
    archive_bytes = BytesIO(build_arcfs([
        ArcFSFile('Draw.thing', b'1'),
        ArcFSFile('DrawDemo', b'2'),
        ArcFSFile('ReadMe', b'3'),
    ]))
    archive = ArcFSArchive(archive_bytes)
    convert_disc_to_zip(archive, str(tmp_path / 'out.zip'), ['Draw'])

    with ZipFile(tmp_path / 'out.zip') as zf:
        assert sorted(zf.namelist()) == ['Draw/thing']