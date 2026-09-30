from datetime import datetime
from io import BytesIO
import os
from pathlib import Path
import shutil
import struct
import subprocess
from zipfile import ZipFile

import pytest

from riscosconv.cli import KnownFileType, extract_riscos_disc, identify_file, load_disc
from riscosconv.riscos_path import PureRiscOsPath
from riscosconv.riscos_zip import RiscOsZip, convert_disc_to_zip
from riscosconv.spark import (
    CT_COMP, CT_CRUNCH, CT_LZNEW, CT_NOTCOMP, CT_NOTCOMP2, CT_PACK, CT_PACKSQUEEZE, CT_SQUASH,
    SparkArchive, SparkError, dos_datetime,
)

from arcfs_builder import compress_lzw
from spark_builder import SparkDir, SparkEnd, SparkFile, build_spark, dos_date_time
from test_arcfs import random_data, runs_data, text_data


ALL_METHODS = [CT_NOTCOMP, CT_NOTCOMP2, CT_PACK, CT_CRUNCH, CT_SQUASH, CT_COMP]
METHOD_IDS = ['store_old', 'store', 'pack', 'crunch', 'squash', 'compress']

SAMPLE_DATA = {
    'empty': b'',
    'one_byte': b'!',
    'runmark': b'\x90',
    'text': text_data(5000),
    'random': random_data(20000),
    'runs': runs_data(),
}


def open_archive(data: bytes) -> SparkArchive:
    return SparkArchive(BytesIO(data))


# --- archive reading tests ---

@pytest.mark.parametrize('method', ALL_METHODS, ids=METHOD_IDS)
@pytest.mark.parametrize('data', SAMPLE_DATA.values(), ids=SAMPLE_DATA.keys())
def test_extract_each_method(method, data):
    archive = open_archive(build_spark([SparkFile('File', data, method=method)]))
    assert archive.read('File') == data
    with archive.open('File') as f:
        assert f.read() == data


@pytest.mark.parametrize('method', [CT_COMP, CT_CRUNCH])
@pytest.mark.parametrize('maxbits', [9, 12, 16])
def test_extract_maxbits(method, maxbits):
    data = text_data(30000, seed=maxbits) + random_data(5000)
    archive = open_archive(build_spark([SparkFile('File', data, method=method, maxbits=maxbits)]))
    assert archive.read('File') == data


def test_old_stored_method_has_no_original_length():
    # method 1 headers are 4 bytes shorter; the length is the compressed length
    archive = open_archive(build_spark([
        SparkFile('Old', b'old data', method=CT_NOTCOMP),
        SparkFile('New', b'new data', method=CT_NOTCOMP2),
    ]))
    assert archive.get_file_meta('Old').file_size == 8
    assert archive.read('Old') == b'old data'
    assert archive.read('New') == b'new data'


def test_list_metadata():
    ts = datetime(1997, 12, 8, 14, 55, 30, 510000)
    archive = open_archive(build_spark([
        SparkFile('Templates', b'template data', filetype=0xfec, timestamp=ts, attr=0x0b),
    ]))
    [(path, meta)] = list(archive.list())
    assert path == PureRiscOsPath('Templates')
    assert meta.file_size == 13
    # the load/exec datestamp is used in preference to the 2 second DOS one
    assert meta.timestamp == ts
    assert meta.ro_meta.filetype == 0xfec
    assert meta.ro_meta.datestamp == ts
    assert meta.ro_meta.file_attr == 0x0b
    meta2 = archive.get_file_meta('Templates')
    assert (meta2.file_size, meta2.timestamp) == (meta.file_size, meta.timestamp)
    assert (meta2.ro_meta.load_addr, meta2.ro_meta.exec_addr) == (meta.ro_meta.load_addr, meta.ro_meta.exec_addr)


def test_untyped_file_uses_dos_date():
    ts = datetime(1993, 2, 3, 4, 5, 6)
    archive = open_archive(build_spark([
        SparkFile('Code', b'\x00' * 16, load_exec=(0x00008000, 0x00008040), timestamp=ts),
    ]))
    meta = archive.get_file_meta('Code')
    assert meta.ro_meta.load_addr == 0x8000
    assert meta.ro_meta.exec_addr == 0x8040
    assert meta.ro_meta.filetype is None
    assert meta.ro_meta.hostfs_file_ext() == ',00008000-00008040'
    # no datestamp in load/exec, so the header's DOS date is used
    assert meta.timestamp == ts


def test_untyped_file_with_invalid_dos_date():
    archive = open_archive(build_spark([
        SparkFile('Code', b'x', load_exec=(0x8000, 0x8000), dos_date_time=(0, 0)),
    ]))
    assert isinstance(archive.get_file_meta('Code').timestamp, datetime)


def test_pc_style_header():
    ts = datetime(1995, 7, 20, 13, 30, 12)
    archive = open_archive(build_spark([
        SparkFile('README.TXT', b'pc file', method=CT_CRUNCH, riscos=False, timestamp=ts),
        SparkFile('NEXT', b'more', method=CT_PACK, riscos=False, timestamp=ts),
    ]))
    # '.' can't be in a RISC OS leafname
    assert list(archive.entries) == [PureRiscOsPath('README_TXT'), PureRiscOsPath('NEXT')]
    meta = archive.get_file_meta('README_TXT')
    assert meta.timestamp == ts
    assert meta.ro_meta.filetype == 0xfff
    assert meta.ro_meta.datestamp == ts
    assert archive.read('README_TXT') == b'pc file'
    assert archive.read('NEXT') == b'more'


def test_dos_datetime():
    dt = datetime(2001, 12, 31, 23, 59, 58)
    assert dos_datetime(*dos_date_time(dt)) == dt
    assert dos_datetime(0, 0) is None    # day 0, month 0


def test_nested_directories():
    archive = open_archive(build_spark([
        SparkDir('!App', [
            SparkFile('!Run', b'Run <Obey$Dir>.!RunImage', filetype=0xfeb),
            SparkDir('Resources', [
                SparkDir('UK', [SparkFile('Messages', b'msgs')]),
                SparkFile('Sprites', b'spr', filetype=0xff9),
            ]),
            SparkFile('!RunImage', text_data(2000), filetype=0xff8),
        ]),
        SparkFile('ReadMe', b'read me'),
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
    archive = open_archive(build_spark([
        SparkDir('Empty'),
        SparkDir('Dir', [SparkDir('AlsoEmpty'), SparkFile('File', b'x')]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Dir.File')]


def test_empty_archive():
    archive = open_archive(build_spark([]))
    assert list(archive.list()) == []


def test_no_end_marker():
    # nspark treats EOF between entries as the end of the archive
    archive = open_archive(build_spark([
        SparkDir('Dir', [SparkFile('A', b'a')]),
        SparkFile('B', b'b'),
    ], end_marker=False))
    assert list(archive.entries) == [PureRiscOsPath('Dir.A'), PureRiscOsPath('B')]


def test_trailing_data_after_end_marker_ignored():
    archive = open_archive(build_spark([SparkFile('A', b'a')]) + b'\x00junk\x1a\xff')
    assert list(archive.entries) == [PureRiscOsPath('A')]


def test_extra_end_marker_at_top_level_stops_parsing():
    archive = open_archive(build_spark([SparkFile('A', b'a'), SparkEnd(), SparkFile('B', b'b')]))
    assert list(archive.entries) == [PureRiscOsPath('A')]


def test_unclosed_directory():
    archive = open_archive(build_spark([SparkDir('Dir', [SparkFile('A', b'a')])], end_marker=False)[:-2])
    assert list(archive.entries) == [PureRiscOsPath('Dir.A')]


def test_duplicate_paths_last_wins():
    archive = open_archive(build_spark([SparkFile('A', b'first'), SparkFile('A', b'second')]))
    assert list(archive.entries) == [PureRiscOsPath('A')]
    assert archive.read('A') == b'second'


def test_name_control_char_ends_name():
    archive = open_archive(build_spark([
        SparkDir(b'Docs\x00e', [SparkFile(b'Changes\x01\xff', b'c')]),
    ]))
    assert list(archive.entries) == [PureRiscOsPath('Docs.Changes')]


def test_name_thirteen_chars():
    archive = open_archive(build_spark([SparkFile('ThirteenChars', b'x')]))
    assert list(archive.entries) == [PureRiscOsPath('ThirteenChars')]


def test_name_with_slash_colon_and_latin1():
    # '/' (like nspark) and ':' can't appear in a RISC OS leafname
    archive = open_archive(build_spark([SparkFile(b'read/me:\xa3', b'x')]))
    assert list(archive.entries) == [PureRiscOsPath('read_me_£')]


def test_empty_name():
    with pytest.raises(SparkError, match='empty file name'):
        open_archive(build_spark([SparkFile(b'\x00', b'x')]))


def test_directory_length_fields_cover_contents():
    # real archives record the size of a directory's contents in its header,
    # but the contents are still parsed as entries (like nspark)
    data = build_spark([SparkDir('Dir', [SparkFile('A', b'a'), SparkFile('B', b'b')]), SparkFile('C', b'c')])
    assert struct.unpack_from('<I', data, 15)[0] > 0
    archive = open_archive(data)
    assert list(archive.entries) == [PureRiscOsPath('Dir.A'), PureRiscOsPath('Dir.B'), PureRiscOsPath('C')]


def test_open_missing_path():
    archive = open_archive(build_spark([SparkFile('A', b'a')]))
    assert archive.open('B') is None


def test_disc_name_and_repr(tmp_path):
    path = tmp_path / 'Test,ddc'
    path.write_bytes(build_spark([]))
    with open(path, 'rb') as fd:
        archive = SparkArchive(fd)
    assert archive.disc_name == 'Test,ddc'
    assert repr(archive) == 'Spark Archive Test,ddc'


def test_reads_from_start_of_file():
    # handler must rewind fd, e.g. after file type identification
    fd = BytesIO(build_spark([SparkFile('A', b'a')]))
    fd.seek(20)
    assert SparkArchive(fd).read('A') == b'a'


# --- CRC and error handling ---

def test_crc_mismatch():
    archive = open_archive(build_spark([SparkFile('File', b'hello', crc=0x1234)]))
    with pytest.raises(SparkError, match='CRC'):
        archive.read('File')


def test_crc_zero_is_checked():
    # unlike ArcFS, Spark always stores a CRC
    archive = open_archive(build_spark([SparkFile('File', b'hello', crc=0)]))
    with pytest.raises(SparkError, match='CRC'):
        archive.read('File')


def test_decompressed_too_short():
    archive = open_archive(build_spark([
        SparkFile('File', b'hello world', method=CT_COMP, comp_data=bytes([12]) + compress_lzw(b'hello', 12)),
    ]))
    with pytest.raises(SparkError, match='expected 11 bytes'):
        archive.read('File')


def test_decompressed_output_truncated_to_length():
    # extra trailing output is discarded, as nspark stops writing at the original length
    data = b'hello'
    archive = open_archive(build_spark([
        SparkFile('File', data, method=CT_NOTCOMP2, comp_data=data + b'\x00\x00'),
    ]))
    assert archive.read('File') == data


@pytest.mark.parametrize('method', [CT_CRUNCH, CT_COMP])
@pytest.mark.parametrize('maxbits', [0, 8, 17])
def test_invalid_maxbits(method, maxbits):
    archive = open_archive(build_spark([
        SparkFile('File', b'x', method=method, comp_data=bytes([maxbits, 0, 0])),
    ]))
    with pytest.raises(SparkError, match='max bits'):
        archive.read('File')


@pytest.mark.parametrize('method', [CT_CRUNCH, CT_COMP])
def test_no_compressed_data(method):
    archive = open_archive(build_spark([
        SparkFile('Empty', b'', method=method, comp_data=b''),
        SparkFile('NotEmpty', b'x', method=method, comp_data=b''),
    ]))
    assert archive.read('Empty') == b''
    with pytest.raises(SparkError, match='expected 1 bytes'):
        archive.read('NotEmpty')


def test_compressed_data_truncated():
    data = build_spark([SparkFile('File', text_data(1000))], end_marker=False)
    archive = open_archive(data[:-10])
    assert list(archive.entries) == [PureRiscOsPath('File')]
    with pytest.raises(SparkError, match='truncated'):
        archive.read('File')


def test_truncated_header():
    data = build_spark([SparkFile('A', b'a'), SparkFile('B', b'b')], end_marker=False)
    first_len = len(build_spark([SparkFile('A', b'a')], end_marker=False))
    with pytest.raises(SparkError, match='truncated Spark header'):
        open_archive(data[:first_len + 20])
    with pytest.raises(SparkError, match='truncated Spark header'):
        open_archive(data[:first_len + 1])


def test_bad_start_byte():
    data = build_spark([SparkFile('A', b'a')], end_marker=False)
    with pytest.raises(SparkError, match=f'bad archive header at offset {len(data)}'):
        open_archive(data + b'\x00junk')


def test_not_a_spark_archive():
    with pytest.raises(SparkError, match='bad archive header at offset 0'):
        open_archive(b'Archive\x00' + bytes(100))


@pytest.mark.parametrize('method', [CT_PACKSQUEEZE, CT_LZNEW, 0x05, 0x07, 0x7e])
def test_unsupported_method(method):
    archive = open_archive(build_spark([
        SparkFile('Squeezed', b'hello', method=method, comp_data=b'hello'),
        SparkFile('After', b'after', method=CT_NOTCOMP2),
    ]))
    # still listed, and the entries after it are still readable
    assert list(archive.entries) == [PureRiscOsPath('Squeezed'), PureRiscOsPath('After')]
    with pytest.raises(SparkError, match=f'unsupported compression method {method}'):
        archive.read('Squeezed')
    assert archive.read('After') == b'after'


# --- CLI integration ---

def sample_archive() -> bytes:
    return build_spark([
        SparkDir('!App', [
            SparkFile('!Boot', b'Set App$Dir <Obey$Dir>', filetype=0xfeb, method=CT_NOTCOMP2),
            SparkFile('!RunImage', text_data(3000), filetype=0xff8, method=CT_CRUNCH),
            SparkDir('User', [SparkFile('Prefs', runs_data(), method=CT_PACK)]),
        ]),
        SparkFile('ReadMe', text_data(800, seed=5), method=CT_SQUASH),
    ])


@pytest.fixture
def sample_path(tmp_path) -> Path:
    path = tmp_path / 'sample,ddc'
    path.write_bytes(sample_archive())
    return path


def test_identify_file(sample_path):
    with open(sample_path, 'rb') as fd:
        assert identify_file(str(sample_path), fd) == KnownFileType.SPARK_ARCHIVE


def test_load_disc(sample_path):
    archive = load_disc(str(sample_path))
    assert isinstance(archive, SparkArchive)
    assert len(list(archive.list())) == 4


def test_extract(sample_path, tmp_path):
    out = tmp_path / 'out'
    out.mkdir()
    extract_riscos_disc(load_disc(str(sample_path)), str(out))

    # more than one item in the root, so extracted into a directory
    root = out / 'sample,ddc'
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
    assert 'file type SPARK_ARCHIVE' in out
    assert 'Spark Archive sample,ddc' in out
    assert 'Absolute ff8    3000 1998-06-14 17:00:52 !App.!RunImage' in out
    assert 'ReadMe' in out


def test_works_without_nspark_installed(sample_path, monkeypatch):
    # Spark support used to shell out to nspark
    monkeypatch.setenv('PATH', '')
    assert len(list(load_disc(str(sample_path)).list())) == 4


# --- optional cross-check against nspark ---

NSPARK = os.environ.get('NSPARK') or shutil.which('nspark')


@pytest.mark.skipif(not NSPARK, reason='nspark not installed (or set NSPARK to its path)')
@pytest.mark.parametrize('method', ALL_METHODS, ids=METHOD_IDS)
def test_nspark_extracts_generated_archive(method, tmp_path):
    # checks the test builder's encoders and header layout against an independent implementation
    files = {
        'Text': text_data(20000, seed=3),
        'Random': random_data(20000, seed=4),
        'Runs': runs_data(),
        'Empty': b'',
    }
    archive_path = tmp_path / 'test,ddc'
    archive_path.write_bytes(build_spark([
        SparkDir('Dir', [SparkDir('Sub', [SparkFile('Nested', b'nested', method=method)])]
                 + [SparkFile(name, data, method=method) for name, data in files.items()]),
        SparkFile('Top', b'top', method=method),
    ]))
    out = tmp_path / 'out'
    out.mkdir()
    result = subprocess.run([NSPARK, '-x', str(archive_path)], cwd=out, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert 'failed' not in result.stderr, result.stderr
    for name, data in files.items():
        assert (out / 'Dir' / name).read_bytes() == data, name
    assert (out / 'Dir' / 'Sub' / 'Nested').read_bytes() == b'nested'
    assert (out / 'Top').read_bytes() == b'top'


@pytest.mark.skipif(not NSPARK, reason='nspark not installed (or set NSPARK to its path)')
def test_matches_nspark_on_generated_archive(tmp_path):
    archive_path = tmp_path / 'test,ddc'
    archive_path.write_bytes(sample_archive())
    out = tmp_path / 'nspark_out'
    out.mkdir()
    subprocess.run([NSPARK, '-xT', str(archive_path)], cwd=out, capture_output=True, check=True)
    ours = SparkArchive(open(archive_path, 'rb'))
    for path, meta in ours.list():
        # nspark appends the filetype in upper case
        theirs = out / (path.as_zipname() + meta.ro_meta.hostfs_file_ext().upper())
        assert theirs.read_bytes() == ours.read(path), path


# --- optional real-world archive ---

REAL_ARCHIVE = os.environ.get('RISCOS_CONV_SPARK_SAMPLE')


@pytest.mark.skipif(not REAL_ARCHIVE, reason='set RISCOS_CONV_SPARK_SAMPLE to a real Spark archive')
def test_real_archive():
    with open(REAL_ARCHIVE, 'rb') as fd:
        archive = SparkArchive(fd)
    paths = [path for path, _ in archive.list()]
    assert paths
    for path in paths:
        # raises on CRC or length mismatch
        archive.read(path)
