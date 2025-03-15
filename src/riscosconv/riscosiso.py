import sys
import struct
import pycdlib
import pycdlib.dr
import os.path
import os
from pycdlib.dr import DirectoryRecord
import shutil

# https://stackoverflow.com/a/57208916
pycdlib.dr.DirectoryRecord = type(
    'DirectoryRecord', 
    (pycdlib.dr.DirectoryRecord,), 
    {'__slots__': ('_raw',)})

# Wrap DirectoryRecord.parse with a method that stores the raw 
real_dr_parse = DirectoryRecord.parse

def new_dr_parse(*args, **kwargs):
    self, vd, record, parent = args
    self._raw = record
    real_dr_parse(*args, **kwargs)

DirectoryRecord.parse = new_dr_parse

def is_iso9660(fd):
    offset = fd.tell()
    fd.seek(0x8001)
    magic = fd.read(5)
    fd.seek(offset)
    return magic == b'CD001'

def is_riscos_iso9660(fd):
    if not is_iso9660(fd):
        return False
    iso = pycdlib.PyCdlib()
    iso.open_fp(f)
    dr = next(dr for dr in iso.list_children(iso_path='/') if dr.is_file())
    return dr.get_riscos_meta() is not None


# http://justsolve.archiveteam.org/wiki/ARCHIMEDES_ISO_9660_extension
def get_riscos_meta(self):
    from riscosconv import RiscOsFileMeta
    meta = self._raw[33+self.len_fi:]
    arc_offset = meta.find(b'ARCHIMEDES')
    if arc_offset < 0:
        return
    arc_data = meta[arc_offset:]
    assert len(arc_data) == 32
    load_addr, exec_addr, attrs = struct.unpack('<III', arc_data[10:22])
    #if attrs & 0x100:
    #    self.file_ident = b'!' + self.file_ident[1:]
    return RiscOsFileMeta(load_addr, exec_addr, attrs)
    
DirectoryRecord.get_riscos_meta = get_riscos_meta

if __name__ == '__main__':
    with open(sys.argv[1], 'rb') as f:

        print(is_riscos_iso9660(f))
        iso = pycdlib.PyCdlib()
        iso.open_fp(f)

        for dir_path,subdirs,files in iso.walk(joliet_path='/'):
            for f in files:
                full_path = os.path.join(dir_path, f)
                dr = iso.get_record(joliet_path=full_path)
                ro_meta = dr.get_riscos_meta()
               
                os.makedirs('.'+dir_path, exist_ok=True)
                out_file = '.' +full_path + ro_meta.hostfs_file_ext()
                print(out_file)
                with iso.open_file_from_iso(joliet_path=full_path) as iso_fd:
                    with open(out_file, 'wb') as out_fd:
                        shutil.copyfileobj(iso_fd, out_fd)

                
                print(full_path, ro_meta.hostfs_file_ext())
