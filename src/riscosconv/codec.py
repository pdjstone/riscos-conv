"""
Shared codecs for the ARC family of archive formats.

ArcFS (arcfs.py) and Spark (spark.py) are both variants of the PC ARC
format and share the same CRC-16, run-length and LZW routines.  They are
ports of nspark's crc.c, pack.c and compress.c by Andrew Brooks, David
Duplain et al.
"""

RUNMARK = 0x90
SQUASH_BITS = 13


class CodecError(Exception):
    pass


def _make_crc_table():
    table = []
    for i in range(256):
        crc = i
        for _ in range(8):
            crc = (crc >> 1) ^ 0xa001 if crc & 1 else crc >> 1
        table.append(crc)
    return table


_CRC_TABLE = _make_crc_table()

def arc_crc16(data: bytes) -> int:
    crc = 0
    for b in data:
        crc = (crc >> 8) ^ _CRC_TABLE[(crc ^ b) & 0xff]
    return crc


def unpack_rle(data: bytes) -> bytes:
    """Undo ARC run-length encoding: <byte> 0x90 <count> repeats byte count times, 0x90 0x00 is a literal 0x90"""
    out = bytearray()
    prev = 0
    running = False
    for b in data:
        if running:
            if b == 0:
                out.append(RUNMARK)
            else:
                out.extend(bytes((prev,)) * (b - 1))
            running = False
        elif b == RUNMARK:
            running = True
        else:
            prev = b
            out.append(b)
    return bytes(out)


def uncompress_lzw(data: bytes, maxbits: int) -> bytes:
    """
    Decompress Unix compress style LZW data (no header), as used by ARC/ArcFS
    crunch, squash and compress methods. Codes are read in groups of n_bits bytes,
    discarding any leftover bits in a group whenever the code size changes.
    """
    INIT_BITS = 9
    CLEAR = 256
    FIRST = 257

    if not INIT_BITS <= maxbits <= 16:
        raise CodecError(f'invalid LZW max bits: {maxbits}')

    maxmaxcode = (1 << maxbits) - 1
    prefix = [0] * (1 << maxbits)
    suffix = list(range(256)) + [0] * ((1 << maxbits) - 256)

    n_bits = INIT_BITS
    maxcode = (1 << n_bits) - 1
    free_ent = FIRST
    clear_flg = False
    pos = 0
    buf_val = 0
    buf_bits = 0   # number of valid code start offsets in buffer
    offset = 0

    def getcode():
        nonlocal n_bits, maxcode, clear_flg, pos, buf_val, buf_bits, offset
        if clear_flg or offset >= buf_bits or free_ent > maxcode:
            if free_ent > maxcode:
                n_bits += 1
                maxcode = maxmaxcode if n_bits == maxbits else (1 << n_bits) - 1
            if clear_flg:
                n_bits = INIT_BITS
                maxcode = (1 << n_bits) - 1
                clear_flg = False
            chunk = data[pos:pos + n_bits]
            if not chunk:
                return -1
            pos += len(chunk)
            buf_val = int.from_bytes(chunk, 'little')
            buf_bits = len(chunk) * 8 - (n_bits - 1)
            offset = 0
            if buf_bits <= 0:
                return -1
        code = (buf_val >> offset) & ((1 << n_bits) - 1)
        offset += n_bits
        return code

    out = bytearray()
    finchar = oldcode = getcode()
    if oldcode == -1:
        return bytes(out)
    out.append(finchar)

    stack = bytearray()
    while (code := getcode()) != -1:
        if code == CLEAR:
            for i in range(256):
                prefix[i] = 0
            clear_flg = True
            free_ent = FIRST - 1
            if (code := getcode()) == -1:
                break
        incode = code
       
        if code >= free_ent:
            stack.append(finchar)
            code = oldcode
        while code >= 256:
            if len(stack) > (1 << 16):
                raise CodecError('corrupt LZW data')
            stack.append(suffix[code])
            code = prefix[code]
        finchar = suffix[code]
        stack.append(finchar)
        stack.reverse()
        out += stack
        stack.clear()

        if free_ent < maxmaxcode:
            prefix[free_ent] = oldcode
            suffix[free_ent] = finchar
            free_ent += 1
        oldcode = incode

    return bytes(out)
