import re
from os.path import dirname


def save_filetypes(filetype_map: dict[str,tuple[str,str]], dst_path: str):
    with open(dst_path, 'w') as f:
        f.write('# This file was auto-generated from filetypes.txt')
        f.write('RISC_OS_FILETYPES = {\n')
        for filetype, (name, desc) in filetype_map.items():
            f.write('  0x{:03x}: ({}, {}),\n'.format(filetype, repr(name), repr(desc)))
        f.write('}\n')


def load_ro_filetypes(filename: str):
    filetype_map = {}
    for l in open(filename, 'r'):
        bits = re.split(r'\t', l.strip(), maxsplit=2)
        if len(bits) == 2:
            bits.append('')
        if len(bits) != 3:
            print(len(bits), l.strip())
        filetype, name, desc = bits
        filetype = int(filetype, 16)
        filetype_map[filetype] = name, desc
    return filetype_map

if __name__ == "__main__":
    src_path = dirname(__file__) + '/filetypes.txt'
    dst_path = dirname(__file__) + '/filetype_list.py'
    filetypes = load_ro_filetypes(src_path)
    save_filetypes(filetypes, dst_path)


