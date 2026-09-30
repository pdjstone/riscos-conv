import io

import pycdlib
import pytest
from helpers import build_iso, make_zip, ro_load
from pycdlib.pycdlibexception import PyCdlibInvalidISO

from riscosconv.indentify import KnownFileType, identify_file
from riscosconv.riscos_iso import RiscOsIsoDisc, is_riscos_iso9660


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
        assert names == ['README', '!ANIM', '!Boot']

        by_name = dict(paths)
        assert by_name['README'].ro_meta.filetype == 0x003
        assert by_name['!Boot'].ro_meta.filetype == 0x0d4
        # Non-&FFF00000 entries have no filetype; hostfs ext falls back to load/exec.
        assert by_name['!ANIM'].ro_meta.filetype is None
        assert by_name['!ANIM'].ro_meta.load_addr == 0x12345678
        assert by_name['!ANIM'].ro_meta.exec_addr == 0x87654321
        assert by_name['!ANIM'].ro_meta.hostfs_file_ext() == ',12345678-87654321'

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

    def test_slash_in_iso_name_maps_to_dot(self):
        # RISC OS long-files discs store entries as NAME/NNN; '/' is a valid
        # RISC OS filename char and maps to '.' for listing/extraction.
        entries = [(b'BLOODS/000', b'data', ro_load(0x003), 1, 3)]
        disc = RiscOsIsoDisc(io.BytesIO(build_iso(entries)))
        names = [p for p, _ in disc.list()]
        assert names == ['BLOODS.000']
        with disc.open('BLOODS.000') as f:
            assert f.read() == b'data'
        assert disc.get_file_meta('BLOODS.000').ro_meta.filetype == 0x003

    def test_multiple_slash_names_list(self):
        entries = [
            (b'BLOODS/000', b'bl', ro_load(0x003), 1, 3),
            (b'BLOODS/001', b'oo', ro_load(0x003), 1, 3),
            (b'WOLFEN/002', b'ds', ro_load(0x003), 1, 3),
        ]
        disc = RiscOsIsoDisc(io.BytesIO(build_iso(entries)))
        names = sorted(p for p, _ in disc.list())
        assert names == ['BLOODS.000', 'BLOODS.001', 'WOLFEN.002']

    def test_pycdlib_rejects_image_falls_back_to_lenient_walker(self, iso_bytes):
        # Corrupt the big-endian path table so pycdlib refuses to open the
        # image; RiscOsIsoDisc must still list and extract via the raw walker.
        img = self.corrupt_be_path_table(iso_bytes)
        with pytest.raises(PyCdlibInvalidISO):
            iso = pycdlib.PyCdlib()
            iso.open_fp(io.BytesIO(img))
        disc = RiscOsIsoDisc(io.BytesIO(img))
        names = sorted(p for p, _ in disc.list())
        assert names == ['!ANIM', '!Boot', 'README']
        with disc.open('README') as f:
            assert f.read() == b'hello world'

    def test_riscos_iso_detected_for_pycdlib_rejected_image(self, iso_bytes):
        # Detection no longer relies on pycdlib, so an image pycdlib refuses
        # to open (corrupt path table) is still classified as a RISC OS ISO.
        img = self.corrupt_be_path_table(iso_bytes)
        assert is_riscos_iso9660(io.BytesIO(img)) is True
        assert identify_file('broken.iso', io.BytesIO(img)) == KnownFileType.RISC_OS_ISO

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