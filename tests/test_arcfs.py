from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import random
import shutil
import struct
import subprocess
from zipfile import ZipFile

import pytest

from riscosconv.arcfs import (
    AFS_COMPRESS, AFS_CRUNCH, AFS_PACK, AFS_SQUASH, AFS_STORE,
    ArcFSArchive, ArcFSError, arc_crc16, uncompress_lzw, unpack_rle,
)
from riscosconv.cli import KnownFileType, extract_riscos_disc, identify_file, load_disc
from riscosconv.riscos_path import PureRiscOsPath
from riscosconv.riscos_zip import RiscOsZip, convert_disc_to_zip

from arcfs_builder import (
    ArcFSDeleted, ArcFSDir, ArcFSEndDir, ArcFSFile, build_arcfs, compress_lzw, pack_rle,
)


ALL_METHODS = [AFS_STORE, AFS_PACK, AFS_CRUNCH, AFS_SQUASH, AFS_COMPRESS]
METHOD_IDS = ['store', 'pack', 'crunch', 'squash', 'compress']


def open_archive(data: bytes) -> ArcFSArchive:
    return ArcFSArchive(BytesIO(data))


def text_data(size: int, seed: int = 0) -> bytes:
    """Compressible, text-like data"""
    rnd = random.Random(seed)
    words = [b'RISC', b'OS', b'Acorn', b'Archimedes', b'ArcFS', b'!Boot', b'\n', b' ', b'\x90']
    out = bytearray()
    while len(out) < size:
        out += rnd.choice(words)
    return bytes(out[:size])


def random_data(size: int, seed: int = 0) -> bytes:
    """Incompressible data, which fills the LZW table quickly"""
    return random.Random(seed).randbytes(size)


def runs_data() -> bytes:
    """Long runs, literal RUNMARKs and runs of RUNMARKs"""
    return b'A' * 300 + b'\x90' + b'B' + b'\x90' * 5 + b'\x00' * 1000 + b'xyz' + b'\x90\x00\x90'


SAMPLE_DATA = {
    'empty': b'',
    'one_byte': b'!',
    'runmark': b'\x90',
    'text': text_data(5000),
    'random': random_data(20000),
    'runs': runs_data(),
}


# --- codec tests ---

@pytest.mark.parametrize('data', SAMPLE_DATA.values(), ids=SAMPLE_DATA.keys())
def test_rle_round_trip(data):
    assert unpack_rle(pack_rle(data)) == data


def test_rle_known_encoding():
    assert unpack_rle(b'A\x90\x05') == b'AAAAA'
    assert unpack_rle(b'\x90\x00') == b'\x90'
    assert unpack_rle(b'AB\x90\x03C') == b'ABBBC'


@pytest.mark.parametrize('maxbits', [9, 12, 13, 16])
@pytest.mark.parametrize('data', SAMPLE_DATA.values(), ids=SAMPLE_DATA.keys())
def test_lzw_round_trip(data, maxbits):
    assert uncompress_lzw(compress_lzw(data, maxbits), maxbits) == data


@pytest.mark.parametrize('maxbits', [9, 12])
def test_lzw_without_clear(maxbits):
    # once the table is full the encoder keeps using it without clearing
    data = text_data(50000, seed=1) + random_data(10000, seed=2)
    assert uncompress_lzw(compress_lzw(data, maxbits, clear_when_full=False), maxbits) == data


def test_lzw_kwkwk():
    # 'aaaa...' exercises the code == free_ent special case
    data = b'a' * 1000
    assert uncompress_lzw(compress_lzw(data, 12), 12) == data


@pytest.mark.parametrize('maxbits', [0, 8, 17])
def test_lzw_invalid_maxbits(maxbits):
    with pytest.raises(ArcFSError, match='max bits'):
        uncompress_lzw(b'\x00\x00', maxbits)


def test_crc16_known_value():
    # CRC-16/ARC check value
    assert arc_crc16(b'123456789') == 0xbb3d
    assert arc_crc16(b'') == 0


# --- archive reading tests ---

@pytest.mark.parametrize('method', ALL_METHODS, ids=METHOD_IDS)
@pytest.mark.parametrize('data', SAMPLE_DATA.values(), ids=SAMPLE_DATA.keys())
def test_extract_each_method(method, data):
    archive = open_archive(build_arcfs([ArcFSFile('File', data, method=method)]))
    assert archive.read('File') == data
    with archive.open('File') as f:
        assert f.read() == data


@pytest.mark.parametrize('method', [AFS_COMPRESS, AFS_CRUNCH])
@pytest.mark.parametrize('maxbits', [9, 12, 16])
def test_extract_maxbits(method, maxbits):
    data = text_data(30000, seed=maxbits) + random_data(5000)
    archive = open_archive(build_arcfs([ArcFSFile('File', data, method=method, maxbits=maxbits)]))
    assert archive.entries[PureRiscOsPath('File')].maxbits == maxbits
    assert archive.read('File') == data


def test_list_metadata():
    ts = datetime(1997, 12, 8, 14, 55, 30, 510000)
    archive = open_archive(build_arcfs([
        ArcFSFile('Templates', b'template data', filetype=0xfec, timestamp=ts, attr=0x0b),
    ]))
    [(path, meta)] = list(archive.list())
    assert path == PureRiscOsPath('Templates')
    assert meta.file_size == 13
    assert meta.timestamp == ts
    assert meta.ro_meta.filetype == 0xfec
    assert meta.ro_meta.datestamp == ts
    assert meta.ro_meta.file_attr == 0x0b
    meta2 = archive.get_file_meta('Templates')
    assert (meta2.file_size, meta2.timestamp) == (meta.file_size, meta.timestamp)
    assert (meta2.ro_meta.load_addr, meta2.ro_meta.exec_addr) == (meta.ro_meta.load_addr, meta.ro_meta.exec_addr)


def test_untyped_file_load_exec():
    archive = open_archive(build_arcfs([
        ArcFSFile('Code', b'\x00' * 16, load_exec=(0x00008000, 0x00008040)),
    ]))
    meta = archive.get_file_meta('Code')
    assert meta.ro_meta.load_addr == 0x8000
    assert meta.ro_meta.exec_addr == 0x8040
    assert meta.ro_meta.filetype is None
    assert meta.ro_meta.hostfs_file_ext() == ',00008000-00008040'
    # no datestamp in load/exec, so falls back to now
    assert isinstance(meta.timestamp, datetime)


def test_nested_directories():
    archive = open_archive(build_arcfs([
        ArcFSDir('!App', [
            ArcFSFile('!Run', b'Run <Obey$Dir>.!RunImage', filetype=0xfeb),
            ArcFSDir('Resources', [
                ArcFSDir('UK', [ArcFSFile('Messages', b'msgs')]),
                ArcFSFile('Sprites', b'spr', filetype=0xff9),
            ]),
            ArcFSFile('!RunImage', text_data(2000), filetype=0xff8),
        ]),
        ArcFSFile('ReadMe', b'read me'),
    ]))
    assert list(archive.entries) == [
        PureRiscOsPath('!App.!Run'),
        PureRiscOsPath('!App.Resources.UK.Messages'),
        PureRiscOsPath('!App.Resources.Sprites'),
        PureRiscOsPath('!App.!RunImage'),
        PureRiscOsPath('ReadMe'),
    ]
    assert archive.read('!App.Resources.UK.Messages') == b'msgs'
    assert archive.read('ReadMe') == b'read me'


def test_empty_directories_not_listed():
    archive = open_archive(build_arcfs([
        ArcFSDir('Empty'),
        ArcFSDir('Dir', [ArcFSDir('AlsoEmpty'), ArcFSFile('File', b'x')]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Dir.File')]


def test_deleted_entries_skipped():
    archive = open_archive(build_arcfs([
        ArcFSDeleted('Gone'),
        ArcFSDir('Dir', [ArcFSDeleted('AlsoGone'), ArcFSFile('Kept', b'kept')]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Dir.Kept')]


def test_trailing_enddir():
    archive = open_archive(build_arcfs([
        ArcFSDir('Dir', [ArcFSFile('A', b'a')]),
        ArcFSFile('B', b'b'),
    ], trailing_enddir=True))
    assert list(archive.entries) == [PureRiscOsPath('Dir.A'), PureRiscOsPath('B')]


def test_enddir_with_stale_name():
    archive = open_archive(build_arcfs([
        ArcFSDir('Dir', [ArcFSFile('A', b'a'), ArcFSEndDir(b'ReadMe')]),
        ArcFSFile('B', b'b'),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Dir.A'), PureRiscOsPath('B')]


def test_empty_archive():
    archive = open_archive(build_arcfs([]))
    assert list(archive.list()) == []


def test_name_with_junk_after_nul():
    archive = open_archive(build_arcfs([
        ArcFSDir(b'Docs\x00e', [ArcFSFile(b'Changes\x00\xff\x01', b'c')]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Docs.Changes')]


def test_name_eleven_chars():
    archive = open_archive(build_arcfs([ArcFSFile('ElevenChars', b'x')]))
    assert list(archive.entries) == [PureRiscOsPath('ElevenChars')]


def test_name_with_slash_and_latin1():
    # '/' cannot appear in a RISC OS leafname; replaced with '_' like nspark
    archive = open_archive(build_arcfs([ArcFSFile(b'read/me\xa3', b'x')]))
    assert list(archive.entries) == [PureRiscOsPath('read_me£')]


def test_ddc_file_is_not_a_directory():
    # a stored Spark archive (&DDC) inside an ArcFS archive is a file, not a dir
    spark = b'\x1a\xff' + bytes(100)
    archive = open_archive(build_arcfs([
        ArcFSDir('Docs', [
            ArcFSFile('Drawfiles', spark, filetype=0xddc, method=AFS_STORE),
            ArcFSFile('Manual', b'manual'),
        ]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Docs.Drawfiles'), PureRiscOsPath('Docs.Manual')]
    assert archive.read('Docs.Drawfiles') == spark
    assert archive.get_file_meta('Docs.Drawfiles').ro_meta.filetype == 0xddc


def test_open_missing_path():
    archive = open_archive(build_arcfs([ArcFSFile('A', b'a')]))
    assert archive.open('B') is None


def test_disc_name_and_repr(tmp_path):
    path = tmp_path / 'Test,3fb'
    path.write_bytes(build_arcfs([]))
    with open(path, 'rb') as fd:
        archive = ArcFSArchive(fd)
    assert archive.disc_name == 'Test,3fb'
    assert repr(archive) == 'ArcFS Archive Test,3fb'


def test_reads_from_current_position():
    # handler must rewind fd, e.g. after file type identification
    fd = BytesIO(build_arcfs([ArcFSFile('A', b'a')]))
    fd.seek(20)
    assert ArcFSArchive(fd).read('A') == b'a'


# --- CRC and error handling ---

def test_crc_mismatch():
    archive = open_archive(build_arcfs([ArcFSFile('File', b'hello', crc=0x1234)]))
    with pytest.raises(ArcFSError, match='CRC'):
        archive.read('File')


def test_crc_zero_not_checked():
    archive = open_archive(build_arcfs([ArcFSFile('File', b'hello', crc=0)]))
    assert archive.read('File') == b'hello'


def test_decompressed_too_short():
    archive = open_archive(build_arcfs([
        ArcFSFile('File', b'hello world', comp_data=compress_lzw(b'hello', 12)),
    ]))
    with pytest.raises(ArcFSError, match='expected 11 bytes'):
        archive.read('File')


def test_decompressed_output_truncated_to_length():
    # extra trailing output (e.g. LZW padding) is discarded
    data = b'hello'
    archive = open_archive(build_arcfs([
        ArcFSFile('File', data, method=AFS_STORE, comp_data=data + b'\x00\x00'),
    ]))
    # complen is taken from comp_data, so the extra bytes are read then dropped
    assert archive.read('File') == data


def test_compressed_data_truncated():
    data = build_arcfs([ArcFSFile('File', text_data(1000))])
    archive = open_archive(data[:-10])
    with pytest.raises(ArcFSError, match='truncated'):
        archive.read('File')


def test_truncated_header():
    data = build_arcfs([ArcFSFile('A', b'a'), ArcFSFile('B', b'b')])
    with pytest.raises(ArcFSError, match='truncated ArcFS header'):
        open_archive(data[:96 + 36 + 10])


def test_bad_magic():
    data = bytearray(build_arcfs([]))
    data[0:8] = b'Archiv\x00\x00'
    with pytest.raises(ArcFSError, match='not an ArcFS archive'):
        open_archive(bytes(data))


def test_too_short():
    with pytest.raises(ArcFSError, match='not an ArcFS archive'):
        open_archive(b'Archive\x00')


def test_newer_version():
    with pytest.raises(ArcFSError, match=r'newer version of ArcFS \(0.41\)'):
        open_archive(build_arcfs([], version=41))


def test_unknown_format():
    with pytest.raises(ArcFSError, match='format 1 not understood'):
        open_archive(build_arcfs([], fmt=1))


def test_unsupported_method():
    archive = open_archive(build_arcfs([ArcFSFile('File', b'x', method=AFS_STORE)]))
    # patch the info byte of the first entry
    data = bytearray(archive.data)
    data[96] = 0x84
    archive = open_archive(bytes(data))
    with pytest.raises(ArcFSError, match='unsupported compression method 0x84'):
        archive.read('File')


def test_builder_header_layout():
    data = build_arcfs([ArcFSFile('A', b'abc', method=AFS_STORE)])
    header_len, data_start, version, rw_version, fmt = struct.unpack_from('<5I', data, 8)
    assert data[:8] == b'Archive\x00'
    assert (header_len, data_start, version, rw_version, fmt) == (36, 132, 40, 100, 0)
    assert data[data_start:] == b'abc'


# --- CLI integration ---

def sample_archive() -> bytes:
    return build_arcfs([
        ArcFSDir('!App', [
            ArcFSFile('!Boot', b'Set App$Dir <Obey$Dir>', filetype=0xfeb, method=AFS_STORE),
            ArcFSFile('!RunImage', text_data(3000), filetype=0xff8, method=AFS_CRUNCH),
            ArcFSDir('User', [ArcFSFile('Prefs', runs_data(), method=AFS_PACK)]),
        ]),
        ArcFSFile('ReadMe', text_data(800, seed=5), method=AFS_SQUASH),
    ])


@pytest.fixture
def sample_path(tmp_path) -> Path:
    path = tmp_path / 'sample.arc'
    path.write_bytes(sample_archive())
    return path


def test_identify_file(sample_path):
    with open(sample_path, 'rb') as fd:
        assert identify_file(str(sample_path), fd) == KnownFileType.ARCFS_ARCHIVE


def test_load_disc(sample_path):
    archive = load_disc(str(sample_path))
    assert isinstance(archive, ArcFSArchive)
    assert len(list(archive.list())) == 4


def test_extract(sample_path, tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    extract_riscos_disc(load_disc(str(sample_path)), str(out))

    # more than one item in the root, so extracted into a directory
    root = out / 'sample'
    files = sorted(str(p.relative_to(root)) for p in root.rglob('*') if p.is_file())
    assert files == ['!App/!Boot,feb', '!App/!RunImage,ff8', '!App/User/Prefs,fff', 'ReadMe,fff']
    assert (root / '!App/!RunImage,ff8').read_bytes() == text_data(3000)
    assert (root / '!App/User/Prefs,fff').read_bytes() == runs_data()
    mtime = datetime.fromtimestamp(os.stat(root / 'ReadMe,fff').st_mtime)
    assert mtime.replace(microsecond=0) == datetime(1998, 6, 14, 17, 0, 52)


def test_convert_to_zip(sample_path, tmp_path):
    zip_path = tmp_path / 'out.zip'
    convert_disc_to_zip(load_disc(str(sample_path)), zip_path, [])

    with ZipFile(zip_path) as zf:
        ro_zip = RiscOsZip(zf.fp)
        listing = dict(ro_zip.list())
        assert list(listing) == [PureRiscOsPath('!App.!Boot'), PureRiscOsPath('!App.!RunImage'), PureRiscOsPath('!App.User.Prefs'), PureRiscOsPath('ReadMe')]
        assert listing[PureRiscOsPath('!App.!RunImage')].ro_meta.filetype == 0xff8
        assert zf.read('ReadMe') == text_data(800, seed=5)


def test_cli_list(sample_path, monkeypatch, capsys):
    from riscosconv import cli
    monkeypatch.setattr('sys.argv', ['riscos-conv', 'l', str(sample_path)])
    cli.cli()
    out = capsys.readouterr().out
    assert 'file type ARCFS_ARCHIVE' in out
    assert 'Absolute ff8    3000 1998-06-14 17:00:52 !App.!RunImage' in out
    assert 'ReadMe' in out


# --- optional cross-check against nspark ---

NSPARK = os.environ.get('NSPARK') or shutil.which('nspark')


@pytest.mark.skipif(not NSPARK, reason='nspark not installed (or set NSPARK to its path)')
@pytest.mark.parametrize('method', ALL_METHODS, ids=METHOD_IDS)
def test_nspark_extracts_generated_archive(method, tmp_path):
    # checks the test builder's encoders against an independent implementation
    files = {
        'Text': text_data(20000, seed=3),
        'Random': random_data(20000, seed=4),
        'Runs': runs_data(),
        'Empty': b'',
    }
    archive_path = tmp_path / 'test.arc'
    archive_path.write_bytes(build_arcfs([
        ArcFSDir('Dir', [ArcFSFile(name, data, method=method) for name, data in files.items()]),
    ]))
    out = tmp_path / 'out'
    out.mkdir()
    result = subprocess.run([NSPARK, '-x', str(archive_path)], cwd=out, capture_output=True, text=True)
    # nspark always reports 'bad archive header' after the last ArcFS entry,
    # and exits non-zero, so check its per-file messages instead
    assert 'failed' not in result.stderr, result.stderr
    for name, data in files.items():
        assert (out / 'Dir' / name).read_bytes() == data, name


# --- optional real-world archive ---

REAL_ARCHIVE = os.environ.get('RISCOS_CONV_ARCFS_SAMPLE')


@pytest.mark.skipif(not REAL_ARCHIVE, reason='set RISCOS_CONV_ARCFS_SAMPLE to a real ArcFS archive')
def test_real_archive():
    with open(REAL_ARCHIVE, 'rb') as fd:
        archive = ArcFSArchive(fd)
    paths = [path for path, _ in archive.list()]
    assert paths
    for path in paths:
        # raises on CRC or length mismatch
        archive.read(path)
