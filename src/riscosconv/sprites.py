
import os
import sys
import array

from os import SEEK_SET
from struct import unpack
from dataclasses import dataclass
from typing import List

from PIL import Image
from PIL.Image import Resampling


@dataclass
class Mode:
    mode: int
    colours: int  # number of colours
    px_width: int  # pixel width in OS units
    px_height: int # pixel height in OS units

    @property
    def ppw(self): # pixels per word
        return COLOURS_TO_PIXELS_PER_WORD[self.colours]

    @property
    def bpp(self): # bits per pixel
        return 32//self.ppw

    
MODES = { m.mode : m for m in (
    Mode(0, colours=2, px_width=2, px_height=4),
    Mode(1, colours=4, px_width=4, px_height=4),
    Mode(4, colours=2, px_width=2, px_height=4),
    Mode(8, colours=4, px_width=2, px_height=4),
    Mode(9, colours=16, px_width=4, px_height=4),
    Mode(12, colours=16, px_width=2, px_height=4),
    Mode(13, colours=256, px_width=4, px_height=4),
    Mode(15, colours=256, px_width=2, px_height=4),
    Mode(18, colours=2, px_width=2, px_height=2),
    Mode(19, colours=4, px_width=2, px_height=2),
    Mode(20, colours=16, px_width=2, px_height=2),
    Mode(21, colours=256, px_width=2, px_height=2),
    Mode(27, colours=16, px_width=2, px_height=2),
    Mode(28, colours=256, px_width=2, px_height=2),
    Mode(31, colours=16, px_width=2, px_height=2),
)}


WIMP_PALETTE_MODE_12 = (
    0xffffff, 0xdddddd, 0xbbbbbb, 0x999999,
    0x777777, 0x555555, 0x333333, 0x000000,
    0x004499, 0xeeee00, 0x00cc00, 0xdd0000,
    0xeeeebb, 0x558800, 0xffbb00, 0x00bbff
)

# Why is the 16-colour WIMP palette different in 256-colour modes?
WIMP_PALETTE_MODE_15 = (
    0xffffff, 0xdddddd, 0xbbbbbb, 0x999999,
    0x777777, 0x555555, 0x333333, 0x000000,
    0x004488, 0xeeee22, 0x00cc00, 0xcc0000,
    0xeeeeaa, 0x448800, 0xffbb33, 0x22aaee
)

# TODO maybe just embed the 64 word palette?
PALETTE_256 = (
    0x000000, 0x111111, 0x222222, 0x333333, 0x440000, 0x551111, 0x662222, 0x773333,
    0x000044, 0x111155, 0x222266, 0x333377, 0x440044, 0x551155, 0x662266, 0x773377,
    0x880000, 0x991111, 0xaa2222, 0xbb3333, 0xcc0000, 0xdd1111, 0xee2222, 0xff3333,
    0x880044, 0x991155, 0xaa2266, 0xbb3377, 0xcc0044, 0xdd1155, 0xee2266, 0xff3377,
    0x004400, 0x115511, 0x226622, 0x337733, 0x444400, 0x555511, 0x666622, 0x777733,
    0x004444, 0x115555, 0x226666, 0x337777, 0x444444, 0x555555, 0x666666, 0x777777,
    0x884400, 0x995511, 0xaa6622, 0xbb7733, 0xcc4400, 0xdd5511, 0xee6622, 0xff7733,
    0x884444, 0x995555, 0xaa6666, 0xbb7777, 0xcc4444, 0xdd5555, 0xee6666, 0xff7777,
    0x008800, 0x119911, 0x22aa22, 0x33bb33, 0x448800, 0x559911, 0x66aa22, 0x77bb33,
    0x008844, 0x119955, 0x22aa66, 0x33bb77, 0x448844, 0x559955, 0x66aa66, 0x77bb77,
    0x888800, 0x999911, 0xaaaa22, 0xbbbb33, 0xcc8800, 0xdd9911, 0xeeaa22, 0xffbb33,
    0x888844, 0x999955, 0xaaaa66, 0xbbbb77, 0xcc8844, 0xdd9955, 0xeeaa66, 0xffbb77,
    0x00cc00, 0x11dd11, 0x22ee22, 0x33ff33, 0x44cc00, 0x55dd11, 0x66ee22, 0x77ff33,
    0x00cc44, 0x11dd55, 0x22ee66, 0x33ff77, 0x44cc44, 0x55dd55, 0x66ee66, 0x77ff77,
    0x88cc00, 0x99dd11, 0xaaee22, 0xbbff33, 0xcccc00, 0xdddd11, 0xeeee22, 0xffff33,
    0x88cc44, 0x99dd55, 0xaaee66, 0xbbff77, 0xcccc44, 0xdddd55, 0xeeee66, 0xffff77,
    0x000088, 0x111199, 0x2222aa, 0x3333bb, 0x440088, 0x551199, 0x6622aa, 0x7733bb,
    0x0000cc, 0x1111dd, 0x2222ee, 0x3333ff, 0x4400cc, 0x5511dd, 0x6622ee, 0x7733ff,
    0x880088, 0x991199, 0xaa22aa, 0xbb33bb, 0xcc0088, 0xdd1199, 0xee22aa, 0xff33bb,
    0x8800cc, 0x9911dd, 0xaa22ee, 0xbb33ff, 0xcc00cc, 0xdd11dd, 0xee22ee, 0xff33ff,
    0x004488, 0x115599, 0x2266aa, 0x3377bb, 0x444488, 0x555599, 0x6666aa, 0x7777bb,
    0x0044cc, 0x1155dd, 0x2266ee, 0x3377ff, 0x4444cc, 0x5555dd, 0x6666ee, 0x7777ff,
    0x884488, 0x995599, 0xaa66aa, 0xbb77bb, 0xcc4488, 0xdd5599, 0xee66aa, 0xff77bb,
    0x8844cc, 0x9955dd, 0xaa66ee, 0xbb77ff, 0xcc44cc, 0xdd55dd, 0xee66ee, 0xff77ff,
    0x008888, 0x119999, 0x22aaaa, 0x33bbbb, 0x448888, 0x559999, 0x66aaaa, 0x77bbbb,
    0x0088cc, 0x1199dd, 0x22aaee, 0x33bbff, 0x4488cc, 0x5599dd, 0x66aaee, 0x77bbff,
    0x888888, 0x999999, 0xaaaaaa, 0xbbbbbb, 0xcc8888, 0xdd9999, 0xeeaaaa, 0xffbbbb,
    0x8888cc, 0x9999dd, 0xaaaaee, 0xbbbbff, 0xcc88cc, 0xdd99dd, 0xeeaaee, 0xffbbff,
    0x00cc88, 0x11dd99, 0x22eeaa, 0x33ffbb, 0x44cc88, 0x55dd99, 0x66eeaa, 0x77ffbb,
    0x00cccc, 0x11dddd, 0x22eeee, 0x33ffff, 0x44cccc, 0x55dddd, 0x66eeee, 0x77ffff,
    0x88cc88, 0x99dd99, 0xaaeeaa, 0xbbffbb, 0xcccc88, 0xdddd99, 0xeeeeaa, 0xffffbb,
    0x88cccc, 0x99dddd, 0xaaeeee, 0xbbffff, 0xcccccc, 0xdddddd, 0xeeeeee, 0xffffff,
)

# Map from no. of colours to default WIMP palette
WIMP_PALETTES = {
    2: (0xffffff, 0),
    4: (0xffffff, 0xbbbbbb, 0x777777, 0),
    16: WIMP_PALETTE_MODE_15,
    256: PALETTE_256
}


COLOURS_TO_PIXELS_PER_WORD = {
    2: 32,
    4: 16,
    16: 8,
    256: 4,
}


class SpriteArea:
    def __init__(self, fd):
        self.fd = fd
        self.num_sprites, self.first_sprite_offset, self.next_free_word = unpack('<III', fd.read(12))
        self._sprite_offsets = None 
    
    def __str__(self):
        return f'SpriteArea(num_sprites={self.num_sprites} next_free=0x{self.next_free_word:x})'

    def sprites(self):
        offset = self.first_sprite_offset - 4
        self.fd.seek(offset)
        while offset < self.next_free_word - 12:
            next_sprite_offset = int.from_bytes(self.fd.read(4), 'little')
            yield Sprite(self.fd)
            offset += next_sprite_offset
            self.fd.seek(offset, SEEK_SET)

    def __getitem__(self, name) -> 'Sprite':
        if not self._sprite_offsets:
            self._sprite_offsets = {s.name.lower() : s.file_offset for s in self.sprites()}
        self.fd.seek(self._sprite_offsets[name.lower()], SEEK_SET)
        return Sprite(self.fd)
         

class PaletteEntry:
    def __init__(self, val):
        self.val = val

    @property
    def r(self):
        return (self.rgb >> 16) & 0xff

    @property
    def g(self):
        return (self.rgb >> 8) & 0xff

    @property
    def b(self):
        return self.rgb & 0xff

    @property
    def rgb(self):
        bgr = self.val & 0xffffffff
        r = (bgr >> 8) & 0xff
        g = (bgr >> 16) & 0xff
        b = bgr >> 24
        return r << 16 | g << 8 | b
        
    def __str__(self):
        return f'({self.val:016x} {self.rgb:06x})'
        

class Palette:
    def __init__(self, data):
        self.palette = array.array('Q', data)

    def __len__(self):
        return len(self.palette)

    def __getitem__(self, n):
        return PaletteEntry(self.palette[n])


class Sprite:
    PALETTE_OFFSET = 44

    def __init__(self, fd):
        self.fd = fd
        self.file_offset = fd.tell()
        self.name = fd.read(12).rstrip(b'\x00').decode('iso8859-1')
        width_words, height, \
            row_first_bit, row_last_bit, \
            self.img_offset, self.mask_offset, self.mode = unpack('<IIIIIII', self.fd.read(4*7))

        self.width_words = width_words + 1
        pixel_width = self.width_words * self.mode_info.ppw
        self.rtrim = 0
        self.ltrim = 0
        if row_last_bit:
            self.rtrim = (31 - row_last_bit)//self.mode_info.bpp
        if row_first_bit:
            self.ltrim = row_first_bit // self.mode_info.bpp 
        self.width = pixel_width - self.rtrim - self.ltrim
        self.height = height + 1
        
    @property
    def mode_info(self):
        try:
            return MODES[self.mode]
        except KeyError:
            raise Exception(f'No mode info for mode {self.mode}')

    @property
    def palette_size(self) -> int:
        return (min(self.img_offset,self.mask_offset)-Sprite.PALETTE_OFFSET)//8

    @property
    def has_palette(self) -> bool:
        return self.palette_size > 0

    @property
    def has_mask(self) -> bool:
        return self.img_offset != self.mask_offset

    @property
    def palette(self):
        if not self.has_palette:
            raise RuntimeError('Sprite does not have palette')
        return Palette(self.palette_data_raw)

    def __str__(self):
        attrs = ''
        if self.has_mask:
            attrs += ' mask'
        if self.has_palette:
            attrs += f' palette({self.palette_size})'
        return f'Sprite(name={self.name} mode={self.mode}{attrs} w={self.width} h={self.height})'

    @property
    def palette_data_raw(self):
        if not self.has_palette:
            return None
        self.fd.seek(self.file_offset + Sprite.PALETTE_OFFSET - 4, SEEK_SET)
        return self.fd.read(8*self.palette_size)

    @property
    def pixel_data_raw(self):
        self.fd.seek(self.file_offset + self.img_offset - 4, SEEK_SET)
        return self.fd.read(self.width_words * 4 * self.height)

    @property
    def mask_data_raw(self):
        if not self.has_mask:
            return None
        self.fd.seek(self.file_offset + self.mask_offset - 4, SEEK_SET)
        return self.fd.read(self.width_words * 4 * self.height)

    def _raw_to_bytearray(self, raw_data: bytes):
        """
        Convert the raw sprite data to a 1-byte per pixel bytearray
        """
        data = array.array('I', raw_data)
        bpp = self.mode_info.bpp
        ppw = self.mode_info.ppw
        pixel_mask = 2**bpp - 1
        max_x = self.width_words * ppw - self.rtrim
        out_data = bytearray(self.width * self.height)

        for i, word in enumerate(data):
            y = i // self.width_words
            wx = i % self.width_words
            for j in range(ppw):
                x = wx*ppw + j
                if x >= max_x:
                    continue
                pixel_val = (word >> (j*bpp)) & pixel_mask
                out_data[y*self.width + x] = pixel_val
        return out_data

    @property
    def pixel_bytes(self):
        return self._raw_to_bytearray(self.pixel_data_raw)

    @property
    def mask_bytes(self):
        return self._raw_to_bytearray(self.mask_data_raw)   

    def get_pil_image(self) -> Image:
        pal = get_rgb_palette(self)
        pixel_data = bytearray(self.width * self.height)
        img = self.pixel_bytes
        mask = None
        if self.has_mask:
            mask = self.mask_bytes
        
        alpha = 0xff
        for i in range(len(pixel_data)):
            if mask:
                alpha = 0xff if mask[i] else 0
            val = (pal[img[i]] << 8) | alpha
            pixel_data[i*4:i*4+4] = val.to_bytes(4, 'big')   
        img = Image.frombytes('RGBA', (self.width, self.height), pixel_data)
    
        if self.mode_info.px_height > self.mode_info.px_width:
            img = img.resize((img.width, img.height * 2), Resampling.NEAREST)

        return img
           

def palette_64_to_rgb(palette: Palette):
    pal = [c.rgb for c in palette]
    for j in range(64, 256, 64):
        for i in range(0,64):
            c = palette[i]
            r = (((j + i) & 0x10) >> 1) | (c.r >> 4)
            g = (((j + i) & 0x40) >> 3) | \
                (((j + i) & 0x20) >> 3) | (c.g >> 4)
            b = (((j + i) & 0x80) >> 4) | (c.b >> 4)
            val = ((r + (r << 4)) << 16) | ((g + (g << 4)) << 8) | (b + (b << 4))
            pal.append(val)
    return pal

def get_rgb_palette(sprite: Sprite) -> List[int]:
    if sprite.has_palette:
        if sprite.mode_info.colours < 256:
            assert len(sprite.palette) == sprite.mode_info.colours
            pal = [c.rgb for c in sprite.palette]
        elif len(sprite.palette) == 64:
            pal = palette_64_to_rgb(sprite.palette)
        elif len(sprite.palette) == 16:
            raise NotImplementedError()
        else:
            raise ValueError(f'Unexpected number of colours in palette: {len(sprite.palette)}')
    else:
        colours = MODES[sprite.mode].colours
        pal = WIMP_PALETTES[colours]
    return pal


if __name__ == '__main__':
    with open(sys.argv[1], 'rb') as f:
        sprite_area = SpriteArea(f)
        print(sprite_area)
        os.makedirs('sprites', exist_ok=True)
        for spr in sprite_area.sprites():
            print(spr)
            spr.get_pil_image().save(f'sprites/{spr.name}.png')

