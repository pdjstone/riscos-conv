import io

import pycdlib
import pytest
from helpers import build_iso, build_joliet_iso, make_zip, ro_load
from pycdlib.pycdlibexception import PyCdlibInvalidISO

from riscosconv.indentify import KnownFileType, identify_file
from riscosconv.riscos_iso import RiscOsIsoDisc, is_riscos_iso9660
from riscosconv.riscos_path import PureRiscOsPath, as_ro_path


class TestRiscOsIso:
    def iso_entries(self):
        return [
            (b'README', b'hello world', ro_load(0x003), 1, 3),
            (b'_Boot', b'*Run !RunImage', ro_load(0x0d4), 2, 0x100 | 3),
            (b'_ANIM', b'DATA\x00\x00', 0x12345678, 0x87654321, 0x100 | 3),
        ]

    @pytest.fixture
    def iso_bytes(self):
        return build_iso(self.iso_entries())

    @pytest.fixture
    def plain_iso_bytes(self):
        # Same image but with every ARCHIMEDES system-use block removed.
        img = bytearray(build_iso(self.iso_entries()))
        i = img.find(b'ARCHIMEDES')
        assert i >= 0
        while i >= 0:
            img[i:i + 32] = b'\0' * 32
            i = img.find(b'ARCHIMEDES', i + 1)
        return bytes(img)

    def corrupt_be_path_table(self, iso_bytes):
        # Flip a byte in the big-endian path table so pycdlib's strict parser
        # refuses to open the image.
        img = bytearray(iso_bytes)
        img[19 * 2048 + 2] ^= 0xff
        return bytes(img)

    def test_riscos_iso_identified(self, iso_bytes):
        assert identify_file('thing.iso', io.BytesIO(iso_bytes)) == KnownFileType.RISC_OS_ISO

    def test_riscos_iso_detected_via_helper(self, iso_bytes):
        assert is_riscos_iso9660(io.BytesIO(iso_bytes))

    def test_plain_iso_is_unknown(self, plain_iso_bytes):
        assert identify_file('pc.iso', io.BytesIO(plain_iso_bytes)) is KnownFileType.UNKNOWN

    def test_non_iso_blob_is_unknown(self):
        assert is_riscos_iso9660(io.BytesIO(b'not an iso at all')) is False

    def test_zipped_riscos_iso(self, iso_bytes):
        data = make_zip({'cover.iso': iso_bytes})
        assert identify_file('cover.iso.zip', io.BytesIO(data)) == KnownFileType.ZIPPED_RISC_OS_ISO

    def test_zipped_plain_iso_is_unknown_not_none(self, plain_iso_bytes):
        # Regression: a zip holding a plain (non-ARCHIMEDES) ISO must classify
        # as UNKNOWN rather than fall off identify_zipfile returning None,
        # which crashed the CLI on '.name'.
        data = make_zip({'pc.iso': plain_iso_bytes})
        assert identify_file('pc.iso.zip', io.BytesIO(data)) == KnownFileType.UNKNOWN

    def test_list_names_and_filetypes(self, iso_bytes):
        disc = RiscOsIsoDisc(io.BytesIO(iso_bytes))
        paths = [(p, m) for p, m in disc.list()]
        names = [p for p, _ in paths]
        # 0x100 attribute prefixes a '_' name with '!'
        assert names == [PureRiscOsPath('README'), PureRiscOsPath('!ANIM'), PureRiscOsPath('!Boot')]

        by_name = {as_ro_path(p): m for p, m in paths}
        assert by_name[as_ro_path('README')].ro_meta.filetype == 0x003
        assert by_name[as_ro_path('!Boot')].ro_meta.filetype == 0x0d4
        # Non-&FFF00000 entries have no filetype; hostfs ext falls back to load/exec.
        assert by_name[as_ro_path('!ANIM')].ro_meta.filetype is None
        assert by_name[as_ro_path('!ANIM')].ro_meta.load_addr == 0x12345678
        assert by_name[as_ro_path('!ANIM')].ro_meta.exec_addr == 0x87654321
        assert by_name[as_ro_path('!ANIM')].ro_meta.hostfs_file_ext() == ',12345678-87654321'

    def test_open_extracts_file(self, iso_bytes):
        disc = RiscOsIsoDisc(io.BytesIO(iso_bytes))
        with disc.open('README') as f:
            assert f.read() == b'hello world'
        with disc.open('!Boot') as f:
            assert f.read() == b'*Run !RunImage'

    def test_get_file_meta(self, iso_bytes):
        disc = RiscOsIsoDisc(io.BytesIO(iso_bytes))
        meta = disc.get_file_meta('!Boot')
        assert meta.ro_meta.filetype == 0x0d4

    def test_unknown_path_returns_none(self, iso_bytes):
        disc = RiscOsIsoDisc(io.BytesIO(iso_bytes))
        assert disc.open('nope') is None
        assert disc.get_file_meta('nope') is None

    def test_slash_in_iso_name_is_a_leafname(self):
        # RISC OS long-files discs store entries as NAME/NNN; '/' is a valid
        # RISC OS filename char, so it is kept in the path (unlike a '.' in a
        # zip leafname, which would map to '/').
        entries = [(b'INDEX/HTM', b'data', ro_load(0x003), 1, 3)]
        disc = RiscOsIsoDisc(io.BytesIO(build_iso(entries)))
        names = [p for p, _ in disc.list()]
        assert names == [PureRiscOsPath('INDEX/HTM')]
        assert names[0].as_zipname() == 'INDEX.HTM'
        with disc.open('INDEX/HTM') as f:
            assert f.read() == b'data'
        assert disc.get_file_meta('INDEX/HTM').ro_meta.filetype == 0x003

    def test_multiple_slash_names_list(self):
        entries = [
            (b'INDEX/HTM', b'bl', ro_load(0x003), 1, 3),
            (b'PHOTO/GIF', b'oo', ro_load(0x003), 1, 3),
            (b'QUUX/TXT', b'ds', ro_load(0x003), 1, 3),
        ]
        disc = RiscOsIsoDisc(io.BytesIO(build_iso(entries)))
        names = sorted(p for p, _ in disc.list())
        assert names == [
            PureRiscOsPath('INDEX/HTM'), PureRiscOsPath('PHOTO/GIF'), PureRiscOsPath('QUUX/TXT'),
        ]

    def test_pycdlib_rejects_image_falls_back_to_lenient_walker(self, iso_bytes):
        # Corrupt the big-endian path table so pycdlib refuses to open the
        # image; RiscOsIsoDisc must still list and extract via the raw walker.
        img = self.corrupt_be_path_table(iso_bytes)
        with pytest.raises(PyCdlibInvalidISO):
            iso = pycdlib.PyCdlib()
            iso.open_fp(io.BytesIO(img))
        disc = RiscOsIsoDisc(io.BytesIO(img))
        names = sorted(str(p) for p, _ in disc.list())
        assert names == ['!ANIM', '!Boot', 'README']
        with disc.open('README') as f:
            assert f.read() == b'hello world'

    def test_riscos_iso_detected_for_pycdlib_rejected_image(self, iso_bytes):
        # Detection no longer relies on pycdlib, so an image pycdlib refuses
        # to open (corrupt path table) is still classified as a RISC OS ISO.
        img = self.corrupt_be_path_table(iso_bytes)
        assert is_riscos_iso9660(io.BytesIO(img)) is True
        assert identify_file('broken.iso', io.BytesIO(img)) == KnownFileType.RISC_OS_ISO

    # -- Joliet display tree --------------------------------------------------

    def test_joliet_dot_name_lists_as_riscos_slash(self):
        # A RISC OS disc mastered for both trees stores RISC OS 'Cover/jpg' as
        # Joliet 'Cover.jpg' (Joliet forbids '/'), so the listed RISC OS path
        # must map the dot back to a type separator, not nest a directory.
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('Cover.jpg', b'img', ro_load(0x003), 1, 3)])))
        names = [(p, m) for p, m in disc.list()]
        assert [(str(p), m.ro_meta.filetype) for p, m in names] == [
            ('Cover/jpg', 0x003),
        ]
        assert names[0][0].parts == ('Cover/jpg',)

    def test_joliet_nested_dirs_dot_leaf(self):
        # Krisalis Games CD (2001): the Joliet tree has real directories
        # AITD/Docs and the file leaf 'Cover.jpg'; the listed RISC OS path is
        # AITD.Docs.Cover/jpg -- the last dot is the type separator, dirs keep
        # their dots as genuine separators.
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('AITD/Docs/Cover.jpg', b'img', ro_load(0x003), 1, 3)])))
        names = [p for p, _ in disc.list()]
        assert [str(p) for p in names] == ['AITD.Docs.Cover/jpg']
        assert names[0].parts == ('AITD', 'Docs', 'Cover/jpg')
        with disc.open('AITD.Docs.Cover/jpg') as f:
            assert f.read() == b'img'
        meta = disc.get_file_meta('AITD.Docs.Cover/jpg')
        assert meta.ro_meta.filetype == 0x003

    def test_joliet_metadata_round_trip(self):
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('Manual.txt', b'hello', ro_load(0x0d4), 2, 3)])))
        by_name = {as_ro_path(p): m for p, m in disc.list()}
        assert by_name[as_ro_path('Manual/txt')].ro_meta.filetype == 0x0d4
        assert by_name[as_ro_path('Manual/txt')].ro_meta.datestamp is not None
        with disc.open('Manual/txt') as f:
            assert f.read() == b'hello'

    def test_joliet_bang_name_and_mixed_caps(self):
        # Joliet stores the literal '!' name (the plain tree maps it to '_',
        # as real discs do); the mixed case survives too.
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('!RunImage', b'*Run !Boot', ro_load(0x0d4), 2, 0x100 | 3)])))
        names = [str(p) for p, _ in disc.list()]
        assert names == ['!RunImage']
        with disc.open('!RunImage') as f:
            assert f.read() == b'*Run !Boot'

    def test_joliet_multi_dot_leaf(self):
        # RISC OS leafnames allow any number of '/' characters; Joliet forbids
        # '/', so a master stores each as '.'.  A leaf 'A.B.jpg' is genuinely
        # RISC OS 'A/B/jpg' -- one leafname with two slashes, not nested dirs.
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('A.B.jpg', b'img', ro_load(0x003), 1, 3)])))
        names = [p for p, _ in disc.list()]
        assert [str(p) for p in names] == ['A/B/jpg']
        assert names[0].parts == ('A/B/jpg',)
        assert names[0].as_zipname() == 'A.B.jpg'

    def test_joliet_consecutive_slash_leaf(self):
        # 'a//b' and '////' are valid RISC OS leafnames; Joliet stores them as
        # 'a..b' and '....' respectively.
        disc = RiscOsIsoDisc(io.BytesIO(build_joliet_iso(
            [('a..b', b'd1', ro_load(0x003), 1, 3),
             ('....', b'd2', ro_load(0x003), 1, 3)])))
        paths = [(str(p), p) for p, _ in disc.list()]
        assert sorted(n for n, _ in paths) == ['////', 'a//b']
        assert dict(paths)['a//b'].as_zipname() == 'a..b'
        assert dict(paths)['////'].as_zipname() == '....'

    def test_archimedes_in_file_content_is_not_a_signature(self):
        # The bare tag string is not reliable: a PC CD-ROM can mention
        # "ARCHIMEDES" inside article text.  Only a tag sitting inside a real
        # directory record's system-use area counts.
        raw = bytearray(build_iso(self.iso_entries()))
        i = raw.find(b'ARCHIMEDES')
        assert i >= 0
        while i >= 0:
            raw[i:i + 32] = b'\0' * 32
            i = raw.find(b'ARCHIMEDES', i + 1)
        raw += b'ARCHIMEDES was a computer company, its 32-bit RISC OS ran '
        raw += b'on Acorn machines until 1998.\n' * 500
        assert is_riscos_iso9660(io.BytesIO(bytes(raw))) is False
        assert identify_file('pc.iso', io.BytesIO(bytes(raw))) == KnownFileType.UNKNOWN