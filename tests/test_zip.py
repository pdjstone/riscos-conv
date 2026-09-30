import io
from zipfile import ZipFile

import pytest
from helpers import make_zip, ro_zip_bytes

from riscosconv.indentify import KnownFileType, identify_file, zip_disc_members


class TestZips:
    def test_plain_zip_is_unknown(self):
        data = make_zip({'x.txt': b'x'})
        assert identify_file('plain.zip', io.BytesIO(data)) == KnownFileType.UNKNOWN

    def test_single_disc_zip(self, adf_bytes):
        data = make_zip({'game.adf': adf_bytes})
        assert identify_file('one.zip', io.BytesIO(data)) == KnownFileType.ZIPPED_DISC_IMAGE

    def test_multi_disc_zip(self, adf_bytes):
        data = make_zip({'a.adf': adf_bytes, 'b.adf': adf_bytes})
        assert identify_file('two.zip', io.BytesIO(data)) == KnownFileType.ZIPPED_MULTI_DISC_IMAGE

    def test_zipped_hfe_disc(self, hfe_bytes):
        data = make_zip({'game.hfe': hfe_bytes})
        assert identify_file('hfe.zip', io.BytesIO(data)) == KnownFileType.ZIPPED_DISC_IMAGE

    def test_riscos_zip(self):
        assert identify_file('app.zip', io.BytesIO(ro_zip_bytes())) == KnownFileType.RISC_OS_ZIP

    def test_zip_disc_members_lists_discs(self, adf_bytes, hfe_bytes):
        data = make_zip({'one.adf': adf_bytes, 'two.hfe': hfe_bytes, 'notes.txt': b'hi'})
        assert zip_disc_members(ZipFile(io.BytesIO(data))) == ['one.adf', 'two.hfe']


class TestIdentifyFile:
    def test_adf_via_identify_file(self, adf_bytes):
        assert identify_file('test.adf', io.BytesIO(adf_bytes)) == KnownFileType.DISC_IMAGE

    def test_hfe_via_identify_file(self, hfe_bytes):
        assert identify_file('test.hfe', io.BytesIO(hfe_bytes)) == KnownFileType.DISC_IMAGE

    def test_single_disc_zip_has_no_member_col(self, adf_bytes):
        data = make_zip({'game.adf': adf_bytes})
        members = zip_disc_members(ZipFile(io.BytesIO(data)))
        assert members == ['game.adf']

    @pytest.mark.parametrize('n', [0, 1, 5, 11])
    def test_tiny_file_is_unknown(self, n):
        # Regression: files < 12 bytes crashed with IndexError/struct.error
        # while checking spark/arcfs/sprite signatures.
        assert identify_file('tiny.txt', io.BytesIO(b'x' * n)) == KnownFileType.UNKNOWN