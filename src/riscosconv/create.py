import os
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from .riscos_zip import get_riscos_zipinfo, zip_extra
from .ro_file_meta import DiscImageBase


def add_file_to_zip(zipfile: ZipFile, filepath: Path, base_path: Path):
    zipinfo = get_riscos_zipinfo(filepath, base_path)
    ro_meta = zipinfo.getRiscOsMeta()
    print(zipinfo.filename, ro_meta)
    with open(filepath, 'rb') as f:
        zipfile.writestr(zipinfo, f.read(), compresslevel=9)

def add_dir_tree_to_zip(zipfile: ZipFile, dirpath: Path, basepath: Path):
    for root, dirs, files in os.walk(dirpath):
        for filename in files:
            filepath = Path(root) / filename
            add_file_to_zip(zipfile, filepath, basepath)
          
def create_riscos_zipfile(zipfile: ZipFile, paths: list[str]|str):
    if type(paths) is str:
        paths = [paths]

    for path in paths:
        path = Path(path)
        if path.is_file():
            add_file_to_zip(zipfile, path, os.path.dirname(path))
        elif path.is_dir():
            dirname = path.name
            basepath = path
            if dirname.startswith('!'):
                basepath = path.parent
            add_dir_tree_to_zip(zipfile, path, basepath)


