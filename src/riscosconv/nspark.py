from io import BytesIO
import os
import subprocess
import shutil
import re
import datetime

from .ro_file_meta import unix_timestamp_to_ro_timestamp, make_load_exec, RiscOsFileMeta, FileMeta, DiscImageBase


if not shutil.which('nspark'):
    raise RuntimeError("nspark not installed")


LISTING_REGEX = r'^([^\s]+)\s+(\d+)\s+(\d+-\w+-\d{4} \d\d:\d\d:\d\d)\s+&(\w+)\s+(\w+)'
                          
class NSparkArchive(DiscImageBase):
    def __init__(self, path_or_fd):
        if hasattr(path_or_fd, 'name'):
            path = path_or_fd.name
        self.path = path
        self._metadata = {}
        self._list()

    @property
    def disc_name(self):
        return os.path.basename(self.path)
    
    def __repr__(self):
        return 'NSpark Archive '+ os.path.basename(self.path)

    def _list(self):
        result = subprocess.run(['nspark', '-lv', self.path], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError('nspark error')
        
        listing = result.stderr.decode('iso-8859-1')
      
        # work around nspark bug where ----- divider doesn't have a newline
        heading_pos = listing.rfind('-------') + 7
        
        listing = listing[heading_pos:]
        for l in listing.splitlines():
            if m := re.match(LISTING_REGEX, l):
                path, file_size, datestr, ft, comp = m.groups()
                dt = datetime.datetime.strptime(datestr, '%d-%b-%Y %H:%M:%S')
                file_size = int(file_size)
                ft = int(ft, 16)
                ro_timestamp = unix_timestamp_to_ro_timestamp(dt.timestamp())
                load_addr, exec_addr = make_load_exec(ft, ro_timestamp)
                self._metadata[path] = FileMeta(RiscOsFileMeta(load_addr, exec_addr), dt, file_size)

    def list(self):
        for path, meta in self._metadata.items():
            yield path, meta

    def open(self, path):
        if path not in self._metadata:
            return None
        result = subprocess.run(['nspark', '-xc', self.path, path], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError('nspark error')
        return BytesIO(result.stdout)
