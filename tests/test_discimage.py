import io

import pytest
from helpers import build_adf, build_adf_big, build_adf_d

from riscosconv.adfs_disc import RiscOsAdfsDisc
from riscosconv.adfslib import ADFSdirectory, ADFSdisc, ADFSfile
from riscosconv.indentify import (KnownFileType, identify_discimage,
                                  identify_file)
from riscosconv.riscos_path import PureRiscOsPath


class TestDiscImages:
    def test_adf_disc_title_parsed(self, adf_bytes):
        # Regression: a directory tail with a title containing a space crashed
        # in _safe (string vs int comparison) while parsing the disc name.
        disc = ADFSdisc(io.BytesIO(adf_bytes))
        assert disc.disc_name == 'Test Disc'

    def test_adf_is_disc_image(self, adf_bytes):
        assert identify_discimage('test.adf', io.BytesIO(adf_bytes)) == KnownFileType.DISC_IMAGE

    def test_adl_is_disc_image(self, adf_bytes):
        assert identify_discimage('test.adl', io.BytesIO(adf_bytes)) == KnownFileType.DISC_IMAGE

    def test_hfe_is_disc_image(self, hfe_bytes):
        assert identify_discimage('test.hfe', io.BytesIO(hfe_bytes)) == KnownFileType.DISC_IMAGE

    @pytest.mark.parametrize('name,data', [
        ('picture.pdf', b'%PDF-1.4\n' + b'\x00' * 1000),
        ('readme.txt', b'hello world\n' * 100),
        ('garbage.bin', bytes(range(256)) * 800),
    ])
    def test_garbage_blobs_are_not_disc_images(self, name, data):
        # Regression: old-format ADFS discs were accepted without a valid
        # catalogue, so arbitrary blobs of the right size looked like discs.
        assert identify_discimage(name, io.BytesIO(data)) == KnownFileType.UNKNOWN

    def test_short_hfe_does_not_close_fd(self):
        # A short image that fails ADFS parsing must leave the caller's
        # handle usable so identify_discimage can fall through to the HFE
        # magic check as a fallback.
        fd = io.BytesIO(b'HXCPICFE' + b'\x00' * 59 + b'\xff')
        assert identify_discimage('x.hfe', fd) == KnownFileType.DISC_IMAGE
        assert not fd.closed

    def test_garbage_does_not_close_fd(self):
        fd = io.BytesIO(b'hello world\n' * 100)
        assert identify_discimage('x.txt', fd) == KnownFileType.UNKNOWN
        assert not fd.closed

    def test_truncated_800k_adf_is_disc_image(self, adf_bytes):
        # arculator (disc.c) loads any .adf up to 1638400 bytes in the
        # 800K/1.6M size bucket regardless of truncation, so preserved
        # dumps with trailing sectors stripped (e.g. 814080 = 819200-5*1024)
        # must still be recognised.
        stripped = adf_bytes[:len(adf_bytes) - 5 * 1024]
        assert identify_file('broken.adf', io.BytesIO(stripped)) == KnownFileType.DISC_IMAGE
    """Old-format ADFS encodes object attributes in the top bits of the
    10 name characters (bit 0=R, 1=W, 2=L, 3=D, 4=E, 5=r, 6=w, 7=e, 8=P).
    The name must have bit 7 stripped so it reads as plain text.
    """

    def disc(self, entries):
        return ADFSdisc(io.BytesIO(build_adf(entries)))

    def names(self, disc):
        return [f.name for f in disc.files]

    def test_file_read_write_bits_stripped(self):
        # '!Boot' (0xa1 0xc2... = 0x21|0x80, 0x42|0x80) with R+W set.
        disc = self.disc([(b'\xa1\xc2oot', 0x1000, 0x1000, 4, 100, 0)])
        assert self.names(disc) == ['!Boot']
        assert isinstance(disc.files[0], ADFSfile)

    def test_directory_bit_4_stripped_and_parsed_as_dir(self):
        # 'ArmProg' = b'Arm' + b'\xd0' + b'rog', D flag at char 3.
        # Pointed at a zeroed sector -> reads as an empty directory.
        disc = self.disc([(b'Arm\xd0rog', 0, 0, 0, 200, 0)])
        assert self.names(disc) == ['ArmProg']
        assert isinstance(disc.files[0], ADFSdirectory)
        assert disc.files[0].files == []

    @pytest.mark.parametrize('name_bytes,expected', [
        (b'!Boot'.replace(b'!', b'\xa1'), '!Boot'),        # R bit
        (b'DoubleTake'.replace(b'D', b'\xc4'), 'DoubleTake'),
        (b'MINDER'.replace(b'M', b'\xcd'), 'MINDER'),
        (b'Pattern'.replace(b'P', b'\xd0'), 'Pattern'),
        (b'ReadMe'.replace(b'R', b'\xd2'), 'ReadMe'),
        (b'Blanc'.replace(b'B', b'\xc2'), 'Blanc'),
    ])
    def test_first_char_bit_stripped(self, name_bytes, expected):
        disc = self.disc([(name_bytes, 0x1000, 0x1000, 4, 100, 0)])
        assert self.names(disc) == [expected]
        assert isinstance(disc.files[0], ADFSfile)

    def test_all_attribute_char_positions_stripped(self):
        # Build a name with each of the 9 attribute bits set in turn
        # ('~' == 0x7e, setting bit 7 yields 0xfe; use 'G' == 0x47 defl 0xc7).
        base = bytearray(b'ABCDEFGHIJ')
        for i in range(5):                 # chars 0..4 attribute bits
            nb = bytearray(base)
            nb[i] |= 0x80
            disc = self.disc([(bytes(nb), 0x1000, 0x1000, 4, 100, 0)])
            assert self.names(disc) == ['ABCDEFGHIJ']
            assert isinstance(disc.files[0], ADFSfile)

    def test_plain_name_without_high_bits_unchanged(self):
        disc = self.disc([(b'Clean', 0x1000, 0x1000, 4, 100, 0)])
        assert self.names(disc) == ['Clean']

    def test_list_yields_stripped_paths(self):
        entries = [
            (b'\xa1\xc2oot', 0x1000, 0x1000, 4, 100, 0),   # !Boot
            (b'\xc4\xefubleTake', 0x1000, 0x1000, 4, 150, 0),  # DoubleTake
        ]
        disc = RiscOsAdfsDisc(io.BytesIO(build_adf(entries)))
        paths = [p for p, _ in disc.list()]
        assert paths == [PureRiscOsPath('!Boot'), PureRiscOsPath('DoubleTake')]

    def test_slash_in_name_is_a_leafname(self):
        # '/' is a valid RISC OS filename char: an on-disc name like
        # 'INDEX/HTM' lists as a single RISC OS leafname, and extraction to a
        # host filesystem escapes the '/' as '.';
        disc = RiscOsAdfsDisc(io.BytesIO(
            build_adf([(b'INDEX/HTM', 0x1000, 0x1000, 4, 100, 0)])))
        paths = [p for p, _ in disc.list()]
        assert paths == [PureRiscOsPath('INDEX/HTM')]
        assert paths[0].as_zipname() == 'INDEX.HTM'

    def test_slash_name_accessible_via_slash_path(self):
        # the '/' -form is the RISC OS path, so get_file_meta/open use it
        disc = RiscOsAdfsDisc(io.BytesIO(
            build_adf([(b'INDEX/HTM', 0x1000, 0x1000, 4, 100, 0)])))
        meta = disc.get_file_meta('INDEX/HTM')
        assert meta.file_size == 4
        with disc.open('INDEX/HTM') as f:
            assert f.read() == b'\x00' * 4

    def test_multiple_slash_names_survive_directory_walk(self):
        entries = [
            (b'INDEX/HTM', 0x1000, 0x1000, 4, 100, 0),
            (b'PHOTO/GIF', 0x1000, 0x1000, 4, 150, 0),
        ]
        disc = RiscOsAdfsDisc(io.BytesIO(build_adf(entries)))
        paths = sorted(p for p, _ in disc.list())
        assert paths == [PureRiscOsPath('INDEX/HTM'), PureRiscOsPath('PHOTO/GIF')]

    def test_truncated_image_lists_present_files(self):
        # A discarded ADF dump with trailing sectors stripped (arculator
        # mounts it as the geometry implied by size) must list what the
        # remaining catalogue describes instead of raising "too short".
        img = build_adf([(b'Test', 0, 0, 1, 100, 0)])
        disc = RiscOsAdfsDisc(io.BytesIO(img[:len(img) - 5 * 256]))
        paths = [p for p, _ in disc.list()]
        assert paths == [PureRiscOsPath('Test')]

    def test_heavy_truncation_still_lists(self):
        img = build_adf([(b'Test', 0, 0, 1, 100, 0)])
        disc = RiscOsAdfsDisc(io.BytesIO(img[:100000]))
        assert [p for p, _ in disc.list()] == [PureRiscOsPath('Test')]

    def test_big_disc_multi_zone_fragment_addresses(self):
        # Regression: find_address_from_map derived the map zone from the
        # file number's top bits, so a file split across zones (0x1A2 here,
        # whose top-7-bit zone is 1) computed the wrong fragment address for
        # every zone.  Fragments must land at (0, 3584) in zone 0 and
        # (1234944, 1237504) in zone 3.
        disc = RiscOsAdfsDisc(io.BytesIO(build_adf_big()))
        assert disc.disc.disc_map[0x1A2] == [(0, 3584), (1234944, 1237504)]

    def test_big_disc_multi_zone_file_opens(self):
        disc = RiscOsAdfsDisc(io.BytesIO(build_adf_big()))
        assert [p for p, _ in disc.list()] == [PureRiscOsPath('BIGFILE')]
        with disc.open('BIGFILE') as f:
            assert f.read() == bytes(i % 256 for i in range(6144))

    def test_d_format_keeps_high_bit_name_char(self):
        # Regression: D format pairs an Old map with a New directory, whose
        # names hold real characters > 127.  Byte 0xBD (a '½') must read back
        # as '½', not be stripped to '=' as S/M/L Old-directory attribute bits.
        disc = RiscOsAdfsDisc(io.BytesIO(build_adf_d()))
        assert disc.disc.disc_type == 'adD'
        assert disc.disc.files[0].name == '!Draw1\u00bd'
        assert [p for p, _ in disc.list()] == [PureRiscOsPath('!Draw1\u00bd')]

    def test_s_format_still_strips_high_bit_name(self):
        # The S/M/L Old-directory attribute encoding must keep stripping the
        # top bit: '!Boot' with R+W set (\xa1\xc2...) reads back as '!Boot'.
        disc = RiscOsAdfsDisc(io.BytesIO(
            build_adf([(b'\xa1\xc2oot', 0x1000, 0x1000, 4, 100, 0)])))
        assert disc.disc.disc_type == 'ads'
        assert disc.disc.files[0].name == '!Boot'

    def test_erased_800k_image_has_empty_catalogue(self):
        # A full-size 800K image wiped to a fill pattern has no catalogue
        # markers and no readable disc record; it must parse to an empty
        # catalogue rather than rejecting the disc outright.
        disc = RiscOsAdfsDisc(io.BytesIO(b'\xa5' * 819200))
        assert disc.disc_name == 'Untitled'
        assert list(disc.list()) == []

    def test_erased_800k_image_still_identifies(self):
        assert identify_discimage('erased.adf',
                                  io.BytesIO(b'\xa5' * 819200)) \
            == KnownFileType.DISC_IMAGE

    def test_truncated_d_image_classified_as_d(self):
        # A full 800K D disc truncated into the L size bucket (e.g. the
        # Talisman and Starch ADFs, preserved dumps with trailing sectors
        # stripped) must still be read as D format.  arculator mounts any
        # .adf up to 819200 bytes at the 800K D geometry, so the root
        # catalogue marker at 0x400 wins over the length-based L guess.
        img = build_adf_d()[:400000]
        disc = ADFSdisc(io.BytesIO(img))
        assert disc.disc_type == 'adD'
        assert disc.files[0].name == '!Draw1\u00bd'
        assert isinstance(disc.files[0], ADFSfile)

    def test_truncated_image_without_d_marker_stays_l(self):
        # Only the D root marker at 0x400 rescues a truncated image from the
        # L bucket; a 640K-range image without it stays L format.
        img = build_adf_d()[:400000]
        img = img[:0x401] + b'\x00' * 4 + img[0x405:]
        disc = ADFSdisc(io.BytesIO(img))
        assert disc.disc_type == 'adl'