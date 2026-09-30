import argparse
import os
import sys
import time
from collections import namedtuple
from typing import Optional
from zipfile import ZipFile

from .adfs_disc import RiscOsAdfsDisc
from .adfslib import ADFSdisc
from .create import create_riscos_zipfile
from .filetypes import RISC_OS_FILETYPES
from .indentify import KnownFileType, has_disc_image_ext, has_iso_ext, identify_file
from .nspark import NSparkArchive
from .riscos_iso import RiscOsIsoDisc
from .riscos_zip import RiscOsZip, convert_disc_to_zip, zip_member_to_tempfile
from .ro_file_meta import DiscImageBase
from .sprites import SpriteArea, list_sprites


def list_disc(disc: DiscImageBase):
    for file_name, file_meta in disc.list():
        ro_meta = file_meta.ro_meta
        ds = file_meta.timestamp
        if ro_meta:
            if ro_meta.filetype:
                name, _ = RISC_OS_FILETYPES.get(ro_meta.filetype, (None, None))
                if name:
                    extra = f'{name} {ro_meta.filetype:03x}'
                else:
                    extra = f'{ro_meta.filetype:03x}'
            else:
                extra = f'{ro_meta.load_addr:08x}-{ro_meta.exec_addr:08x}'
        else:
            extra = ''
        date_formatted = ds.strftime('%Y-%m-%d %H:%M:%S')
        print(f'{extra: >17} {file_meta.file_size: >7} {date_formatted} {file_name}')

def many_files_in_root(disc: DiscImageBase):
    files_in_root = set()
    for file_name, meta in disc.list():
        first = file_name.split('/', 1).pop(0)
        files_in_root.add(first)
    return len(files_in_root) > 1

def extract_riscos_disc(disc: DiscImageBase, path='.'):
    if many_files_in_root(disc):
        name, _ = os.path.splitext(os.path.basename(disc.disc_name))
        path += '/' + name
    print(f'Extracting to {path}:')
    for filename, meta in disc.list():
        ro_meta = meta.ro_meta
        extract_path = os.path.join(path, filename + ro_meta.hostfs_file_ext())
        print(' ', extract_path)
        extract_dir = os.path.dirname(extract_path)
        os.makedirs(extract_dir, exist_ok=True)
        with disc.open(filename) as f, open(extract_path, 'wb') as ff:
            ff.write(f.read())
        ds = meta.timestamp
        if ds:
            ts = time.mktime(ds.timetuple())
            ts_ns = int(ts * 1_000_000_000) + ds.microsecond * 1000
            os.utime(extract_path, ns=(ts_ns,ts_ns))

def extract_riscos_sprites(sprite_area: SpriteArea, path='.'):
    print(f'Extracting to {path}')
    for spr in sprite_area.sprites():
        out_name = f'{path}/{spr.name}.png'
        print(f'  {out_name}')
        spr.get_pil_image().save(out_name)


def load_disc(main_file: str) -> Optional[DiscImageBase]:
    with open(main_file, 'rb') as fd:
        file_type = identify_file(main_file, fd)
    if file_type == KnownFileType.UNKNOWN:
        return None
    fd = open(main_file, 'rb')
    if file_type == KnownFileType.ZIPPED_DISC_IMAGE:
        fd = extract_single_disc_image_from_zip(fd, has_disc_image_ext)
        file_type = KnownFileType.DISC_IMAGE
    elif file_type == KnownFileType.ZIPPED_RISC_OS_ISO:
        fd = extract_single_disc_image_from_zip(fd, lambda n: n.lower().endswith('.iso'), to_temp=True)
        file_type = KnownFileType.RISC_OS_ISO
    riscos_disc = HANDLER_FNS[file_type](fd)
    return riscos_disc

def extract_single_disc_image_from_zip(fd, member_ext=has_disc_image_ext, to_temp=False):
    zipfile = ZipFile(fd, 'r')
    for info in zipfile.infolist():
        if member_ext(info.filename):
            if to_temp:
                return zip_member_to_tempfile(zipfile, info)
            return zipfile.open(info, 'r')
    raise Exception("Did not find single disc image in ZIP file")


def extract_disc_image(fd, path='.'):
    adfs = ADFSdisc(fd)

    if len(adfs.files) > 1:
        path = path + '/' + adfs.disc_name
        os.makedirs(path, exist_ok=True)
    adfs.extract_files(path, with_time_stamps=True, filetypes=True)


HandlerFns = namedtuple('HandlerFns', ['list', 'extract', 'create'], defaults=(None,))

HANDLER_FNS = {
    KnownFileType.DISC_IMAGE: RiscOsAdfsDisc,
    KnownFileType.RISC_OS_ISO: RiscOsIsoDisc,
    KnownFileType.RISC_OS_ZIP: RiscOsZip,
    KnownFileType.ARCFS_ARCHIVE: NSparkArchive,
    KnownFileType.SPARK_ARCHIVE: NSparkArchive,
    KnownFileType.RISC_OS_SPRITES: SpriteArea
}

LIST_FNS = {
    KnownFileType.DISC_IMAGE: list_disc,
    KnownFileType.RISC_OS_ISO: list_disc,
    KnownFileType.RISC_OS_ZIP: list_disc,
    KnownFileType.ARCFS_ARCHIVE: list_disc,
    KnownFileType.SPARK_ARCHIVE: list_disc,
    KnownFileType.RISC_OS_SPRITES: list_sprites
}

EXTRACT_FNS = {
    KnownFileType.DISC_IMAGE: extract_riscos_disc,
    KnownFileType.RISC_OS_ISO: extract_riscos_disc,
    KnownFileType.RISC_OS_ZIP: extract_riscos_disc,
    KnownFileType.ARCFS_ARCHIVE: extract_riscos_disc,
    KnownFileType.SPARK_ARCHIVE: extract_riscos_disc,
    KnownFileType.RISC_OS_SPRITES: extract_riscos_sprites
}

def cli():
    parser = argparse.ArgumentParser(prog='riscos-conv', description="Extract and create RISC OS archive files")
    parser.add_argument('-d', '--dir', default='.', help='Output directory')
    parser.add_argument('-a', '--append', action='store_true', help='Append files to existing archive')
    parser.add_argument('action', choices=['i', 'x','l','c','d2z'], nargs='?', default='l', help='[i]dentify, e[x]tract, [l]ist, [c]reate archive or convert disc to ZIP archive [d2z]')
    parser.add_argument('file', help='ZIP or (zipped) disc file to create or list/extract')
    parser.add_argument('files', nargs='*', help='Files to extract / add')
    args = parser.parse_args()

    main_file = args.file

    if args.action in ('i', 'l', 'x', 'd2z'):
        if not os.path.isfile(main_file):
            sys.stderr.write(f'file not found: {main_file}\n')
            sys.exit(-1)
        fd = open(main_file, 'rb')
        file_type = identify_file(main_file, fd)
      
        if args.action == 'i':
            print(f'{main_file}: {file_type.name}')
            sys.exit(0)

        if file_type == KnownFileType.UNKNOWN:
            sys.stderr.write(f'{main_file}: unknown file type\n')
            sys.exit(-1)

        print(f'file type {file_type.name}')
        if file_type == KnownFileType.ZIPPED_DISC_IMAGE:
            fd = extract_single_disc_image_from_zip(fd, has_disc_image_ext)
            file_type = KnownFileType.DISC_IMAGE
        elif file_type == KnownFileType.ZIPPED_RISC_OS_ISO:
            fd = extract_single_disc_image_from_zip(fd, has_iso_ext, to_temp=True)
            file_type = KnownFileType.RISC_OS_ISO

    elif args.action == 'c':
        if not main_file.lower().endswith('.zip'):
            sys.stderr.write('Only support creating zip files\n')
            sys.exit(-1)
    
    if args.action == 'd2z':
        if file_type not in (KnownFileType.DISC_IMAGE, KnownFileType.ARCFS_ARCHIVE, KnownFileType.SPARK_ARCHIVE):
            sys.stderr.write('Must provide disc image to convert to archive\n')
            sys.exit(-1)
        if len(args.files) == 0:
            sys.stderr.write('Must provide an output ZIP filename\n')
            sys.exit(-1)
        output_zip_path = args.files[0]
        extract_paths = args.files[1:]

   
    match args.action:
        case 'l':
            riscos_disc = HANDLER_FNS[file_type](fd)
            print(riscos_disc)
            LIST_FNS[file_type](riscos_disc)
        case 'x':
            riscos_disc = HANDLER_FNS[file_type](fd)
            print(riscos_disc)
            assert os.path.isdir(args.dir)
            EXTRACT_FNS[file_type](riscos_disc, args.dir)
        case 'c':
            mode = 'w'
            if args.append:
                mode = 'a'
            zip = ZipFile(main_file, mode)
            create_riscos_zipfile(zip, args.files)
        case 'd2z':
            riscos_disc = HANDLER_FNS[file_type](fd)
            print(riscos_disc)
            convert_disc_to_zip(riscos_disc, output_zip_path, extract_paths)


if __name__ == '__main__':
    cli()
