# riscos-conv

A Python utility to list, extract and create some RISC OS file formats.

Supported formats:

* ADFS disc images (list/extract)
* RISC OS ZIP/SparkFS (list/extract/create)
* Spark/ArcFS (list/extract)
* RISC OS Sprite files (list/extract)

Spark and ArcFS files require the `nspark` tool to be installed.
Currently only Sprite files created on RISC OS 3.1 and earlier are supported.

## Usage

`usage: riscos-conv [-h] [-d DIR] [-a] [{x,l,c,d2z}] file [files ...]`

* `x` - Extract archive/disc image/sprite file
* `l` - List archive/disc image/sprite file
* `c` - create a RISC OS-compatible ZIP file
* `d2z` - convert an ADF disc image to a RISC OS ZIP file

### Listing

List a disc/archive file:

```
$ riscos-conv l riscos3-app2.adf 
file type DISC_IMAGE
ADFS Disc - App2
         Obey feb     132 1989-09-08 15:14:09 !65Host/!Boot
         Text fff     157 1992-05-11 11:22:59 !65Host/!Help
         Obey feb    1282 1992-05-18 15:03:14 !65Host/!Run
       Module ffa   64968 1992-05-18 11:59:05 !65Host/!RunImage
       Sprite ff9    1272 1991-06-12 12:21:21 !65Host/!Sprites
...
```

List sprites in a Sprite file:

```
$ riscos-conv l Sprites,ff9
file type RISC_OS_SPRITES
SpriteArea(num_sprites=110 next_free=0x85e8)
  directory (34x17) mode 12
  small_dir (18x9) mode 12
  application (34x17) mode 12
  small_app (18x9) mode 12
  file_xxx (34x17) mode 4
  ...
```

### Extracting files

For ADF disc images, this will create and extract into a directory with the name of the disc.

```
$ riscos-conv x ../archimedes-live/dlcache/riscos3-app2.adf 
file type DISC_IMAGE
ADFS Disc - App2
Extracting to ./App2:
  ./App2/!65Host/!Boot,feb
  ./App2/!65Host/!Help,fff
  ./App2/!65Host/!Run,feb
  ./App2/!65Host/!RunImage,ffa
  ./App2/!65Host/!Sprites,ff9
  ...
```

Extracting sprites:

```
$ riscos-conv x \!Sprites22\,ff9 
file type RISC_OS_SPRITES
SpriteArea(num_sprites=2 next_free=0x768)
Extracting to .
  ./!clock.png
  ./sm!clock.png
```

### Creating a RISC OS ZIP file

Create a RISC OS ZIP from files on disc. Files must have a comma-extension with the
RISC OS hex filetype (e.g. filename,abc)

```
$ riscos-conv c tube.zip \!65Tube/
!65Tube/!RunImage RiscOsFileMeta(type=ffa date=1992-05-18 12:24:32.070000 attr=3)
!65Tube/!Help RiscOsFileMeta(type=fff date=1992-05-11 11:28:41 attr=3)
!65Tube/!Sprites23 RiscOsFileMeta(type=ff9 date=1991-09-11 11:33:20 attr=3)
!65Tube/!Run RiscOsFileMeta(type=feb date=1992-05-18 15:04:48.590000 attr=3)
!65Tube/!Sprites22 RiscOsFileMeta(type=ff9 date=1989-11-10 13:21:06 attr=3)
!65Tube/!Sprites RiscOsFileMeta(type=ff9 date=1990-11-15 17:58:31 attr=3)
!65Tube/!Boot RiscOsFileMeta(type=feb date=1990-07-23 16:22:10.080000 attr=3)
```

### Converting an ADFS disc image to RISC OS ZIP

This will convert a disc image to a ZIP file while retaining the file types and date stamps.

```
$ riscos-conv d2z ~/projects/archimedes-live/dlcache/riscos3-app1.adf app1.zip
file type DISC_IMAGE
ADFS Disc - App1
!FontPrint/!Help RiscOsFileMeta(type=fff date=1992-05-11 10:01:45 attr=3)
!FontPrint/!Run RiscOsFileMeta(type=feb date=1992-05-14 16:18:01 attr=3)
!FontPrint/!RunImage RiscOsFileMeta(type=ff8 date=1992-05-18 17:22:53.220000 attr=3)
!FontPrint/!Sprites RiscOsFileMeta(type=ff9 date=1991-05-31 14:04:57 attr=3)
!FontPrint/!Sprites22 RiscOsFileMeta(type=ff9 date=1991-05-31 14:10:06 attr=3)
!FontPrint/!Sprites23 RiscOsFileMeta(type=ff9 date=1991-05-31 14:20:40 attr=3)
```

If you only want to convert some of the contents of the disc to a ZIP, you can specify one
or more paths from the disc image to archive:

```
$ riscos-conv d2z ~/projects/archimedes-live/dlcache/riscos3-app1.adf app1.zip \!Squash DrawDemo
file type DISC_IMAGE
ADFS Disc - App1
!Squash/!Boot RiscOsFileMeta(type=feb date=1991-05-29 09:57:02 attr=3)
!Squash/!Help RiscOsFileMeta(type=fff date=1992-05-19 15:25:35 attr=3)
!Squash/!Run RiscOsFileMeta(type=feb date=1992-05-14 16:44:00 attr=3)
!Squash/!RunImage RiscOsFileMeta(type=ff8 date=1992-05-19 15:55:22.800000 attr=3)
!Squash/!Sprites RiscOsFileMeta(type=ff9 date=1991-06-12 12:07:32 attr=3)
!Squash/!Sprites22 RiscOsFileMeta(type=ff9 date=1991-06-12 12:08:13 attr=3)
!Squash/!Sprites23 RiscOsFileMeta(type=ff9 date=1991-06-12 12:08:45 attr=3)
!Squash/Messages RiscOsFileMeta(type=fff date=1992-05-19 15:25:45 attr=3)
!Squash/Squash RiscOsFileMeta(type=ff8 date=1992-05-19 15:54:59.330000 attr=3)
!Squash/Templates RiscOsFileMeta(type=fec date=1992-05-14 18:46:04 attr=3)
DrawDemo RiscOsFileMeta(type=aff date=1991-09-06 16:19:36 attr=3)
```
