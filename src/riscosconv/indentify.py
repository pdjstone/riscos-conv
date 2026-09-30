import os
import struct
from enum import Enum
from zipfile import ZipFile, is_zipfile

from .adfslib import ADFS_exception, ADFSdisc
from .riscos_iso import is_iso9660, is_riscos_iso9660
from . import riscos_zip  # noqa: F401  applies the ZipInfo.getRiscOsMeta patch

DISC_IM_EXTS = ('.adf','.adl', '.hfe')


class KnownFileType(Enum):
    RISC_OS_ZIP = 1
    ZIPPED_DISC_IMAGE = 2
    ZIPPED_MULTI_DISC_IMAGE = 3
    DISC_IMAGE = 4
    SPARK_ARCHIVE = 5
    ARCFS_ARCHIVE = 6
    RISC_OS_SPRITES = 7
    UNKNOWN = 8
    RISC_OS_ISO = 9
    ZIPPED_RISC_OS_ISO = 10


def has_disc_image_ext(filename: str) -> bool:
    return any(filename.lower().endswith(ext) for ext in DISC_IM_EXTS)

def has_iso_ext(filename: str) -> bool:
    return filename.lower().endswith('.iso')

def scan_zip_members(zipfile: ZipFile, want_discs=True, want_isos=True):
    """Single pass over a zip's members.

    Counts the members carrying RISC OS file metadata and, when requested,
    collects the names of members that are disc images and/or ARCHIMEDES ISO
    disc images.  Each member is opened at most once.

    Returns (disc_members, iso_members, num_ro_meta).
    """
    disc_members = []
    iso_members = []
    num_ro_meta = 0
    for info in zipfile.infolist():
        if info.getRiscOsMeta():
            num_ro_meta += 1
        try:
            if want_discs and has_disc_image_ext(info.filename):
                with zipfile.open(info, 'r') as item_fd:
                    if identify_discimage(info.filename, item_fd) == KnownFileType.DISC_IMAGE:
                        disc_members.append(info.filename)
            elif want_isos and info.filename.lower().endswith('.iso'):
                with zipfile.open(info, 'r') as item_fd:
                    if identify_isoimage(info.filename, item_fd) == KnownFileType.RISC_OS_ISO:
                        iso_members.append(info.filename)
        except NotImplementedError:
            # Member uses a compression method we can't decode (e.g. deflate64);
            # treat it as not a probeable disc/ISO.
            continue
    return disc_members, iso_members, num_ro_meta


def zip_riscos_iso_members(zipfile: ZipFile) -> list:
    """Return the member filenames of a zip that are ARCHIMEDES ISO disc
    images."""
    _, iso_members, _ = scan_zip_members(zipfile, want_discs=False)
    return iso_members

def zip_disc_members(zipfile: ZipFile) -> list:
    """Return the member filenames of a zip that are disc images."""
    disc_members, _, _ = scan_zip_members(zipfile, want_isos=False)
    return disc_members

def identify_zipfile(zipfile: ZipFile):
    disc_members, iso_members, num_ro_meta = scan_zip_members(zipfile)

    if len(iso_members) >= 1: # for now we don't handle multiple ISOs in a zip
        return KnownFileType.ZIPPED_RISC_OS_ISO
    if len(disc_members) == 1:
        return KnownFileType.ZIPPED_DISC_IMAGE
    if len(disc_members) > 1:
        return KnownFileType.ZIPPED_MULTI_DISC_IMAGE
    if num_ro_meta >= 1:
        return KnownFileType.RISC_OS_ZIP
    return KnownFileType.UNKNOWN


def identify_isoimage(filename: str, fd):
    """Return RISC_OS_ISO if fd is an ISO9660 image with ARCHIMEDES metadata,
    else UNKNOWN."""
    if is_iso9660(fd) and is_riscos_iso9660(fd):
        return KnownFileType.RISC_OS_ISO
    return KnownFileType.UNKNOWN


def identify_discimage(filename: str, fd):
    try:
        disc = ADFSdisc(fd)
    except ADFS_exception:
        pass
    else:
        # The old ADFS formats (<800K) don't validate the root catalog in the
        # constructor, so arbitrary byte blobs of the right length could pass.
        # Only accept a disc whose root directory marker was actually found.
        if disc.root_name or disc.files:
            return KnownFileType.DISC_IMAGE
    fd.seek(0, os.SEEK_SET)

    if fd.read(8) == b'HXCPICFE':  # HFE disc image signature
        return KnownFileType.DISC_IMAGE
    return KnownFileType.UNKNOWN


def identify_file(filename: str, fd) -> KnownFileType:
    if filename.endswith(',ff9'):
        return KnownFileType.RISC_OS_SPRITES
    
    if is_zipfile(fd):
        zipfile = ZipFile(fd)
        return identify_zipfile(zipfile)
    
    fd.seek(0, os.SEEK_SET)
    data = fd.read(12)
    fd.seek(0, os.SEEK_SET)

    # Too small to hold Spark/ArcFS/sprite signatures.
    if len(data) < 12:
        return KnownFileType.UNKNOWN

    # From spark.h in NSpark
    if data[0] == 0x1a and (data[1] & 0xf0 == 0x80 or data[1] == 0xff):
        return KnownFileType.SPARK_ARCHIVE

    if data[0:8] == b'Archive\x00':
        return KnownFileType.ARCFS_ARCHIVE

    num_sprites, first_offset, next_free = struct.unpack('<III', data)
    size = fd.seek(0, os.SEEK_END)
    fd.seek(0, os.SEEK_SET)
    if first_offset == 16 and next_free == size + 4:
        return KnownFileType.RISC_OS_SPRITES

    file_type = identify_isoimage(filename, fd)
    if file_type != KnownFileType.UNKNOWN:
        return file_type
    return identify_discimage(filename, fd)