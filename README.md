# Henneko PSP — Asset Extractor

Extracts dialogue scripts, character voice audio, and image assets (backgrounds, CGs, sprites) from the PSP game:

> **変態王子と笑わない猫。** (Hentai Ouji to Warawanai Neko / "The Hentai Prince and the Stony Cat")

## What This Does

```
Game ISO (.iso)
  ├─ first.dat → text.dat → Name.csv (character table) + voice_list_dst.txt
  ├─ RES.DAT  → script.dat → per-chapter .obj binary scripts (dialogue records)
  │            → script_bg.dat / script_event.dat / script_charactor*.dat → GIM images
  └─ voice.awb → AFS2 container → HCA audio files
```

**Output:**

```
output/
  ├─ scn/                    # Dialogue scripts (one .txt per chapter)
  │   ├─ A_K00_prologue.txt
  │   ├─ A_K01_01_01.txt
  │   └─ ...
  ├─ voice/                  # Voice audio files (.wav)
  │   ├─ YOKO_0000.wav
  │   ├─ TUKI_0000.wav
  │   └─ ...
  └─ images/                 # Extracted images (.png)
      ├─ bg/                 # Backgrounds (480×272)
      ├─ cg/                 # Event CGs (480×272)
      ├─ sprite/             # Character bodies + face overlays
      ├─ face/               # Standalone face sprites
      ├─ eyecatch/           # Episode eye-catch images
      ├─ nameplate/          # Character name plates
      └─ item/               # Character item sprites
```

Each `.txt` file has the format: `voice_name|character_name|dialogue_text`

```
YOKO_0000|横寺|ぼくが敬愛するアイルランドの変態作家、オスカー・ワイルドは、こんな言葉を残している
null|null|良い決心というものには、ひとつの宿命が付きまとっている。
TUKI_0000|月子|はい。何でしょうか
```

- `voice_name` = `null` means narration (no audio)
- `character_name` = `null` means narrator / no speaker

## Requirements

```bash
pip install pycdlib tqdm numpy Pillow
pip install git+https://github.com/Youjose/PyCriCodecs.git  # HCA decoder (optional)
```

- **Python 3.8+**
- `pycdlib` — ISO reading without mounting
- `tqdm` — progress bars
- `numpy` + `Pillow` — image decoding (GIM → PNG)
- `PyCriCodecs` — HCA audio decoding (optional; without it, raw `.hca` files are saved)

## Usage

### Step 1: Extract from ISO

```bash
python scripts/extract.py --iso "path/to/game.iso" --output ./output
```

Options:
| Flag | Description |
|------|-------------|
| `--iso` | Path to the game ISO file (required) |
| `--output` | Output directory (default: `./output`) |
| `--keep-hca` | Save raw HCA audio instead of decoding to WAV |

### Step 2: Extract images (BG, CG, sprites)

```bash
# From ISO directly:
python scripts/extract_images.py --iso "path/to/game.iso" --output ./output/images

# Or from an already-extracted RES.DAT file:
python scripts/extract_images.py --res "path/to/RES.DAT" --output ./output/images
```

Options:
| Flag | Description |
|------|-------------|
| `--iso` | Path to the game ISO file |
| `--res` | Path to extracted RES.DAT file (alternative to --iso) |
| `--output` | Output directory (default: `./output/images`) |
| `--types` | Only extract specific types: `bg cg sprite face item nameplate eyecatch` |

Examples:
```bash
# Extract only backgrounds and CGs:
python scripts/extract_images.py --res RES.DAT --output ./images --types bg cg

# Extract only character sprites:
python scripts/extract_images.py --iso game.iso --output ./images --types sprite
```

### Step 3: Post-process dialogue scripts

The raw extraction may produce some garbled lines due to how the binary scripts are parsed. Run the post-processor to fix them:

```bash
python scripts/postprocess.py --scn-dir ./output/scn
```

This will:
- Remove garbled lines (binary control code artifacts)
- Extract embedded dialogue from multi-line blocks
- Fill in missing character names
- Clean up control code residuals in narration text

## Reverse Engineering Notes

### File Formats

#### GIM (Graphics Image Map — PSP Texture Format)
All image assets use PSP's native GIM format (magic: `MIG.00.1PSP`). Block-based structure:

```
[0x00]  "MIG.00.1PSP\0..."  (16 bytes, file header)
[0x10]  Block tree:
        Block header (16 bytes each):
          type(u16)  unk(u16)  size(u32)  next_offset(u32)  data_offset(u32)
        Types:
          0x02 = Root container
          0x03 = Image container
          0x04 = Pixel data block
          0x05 = Palette data block

        Pixel/Palette sub-header (0x40 bytes):
          +0x04: format(u16)  — 0x00=RGB565, 0x01=RGBA5551, 0x02=RGBA4444,
                                 0x03=RGBA8888, 0x04=index4, 0x05=index8
          +0x06: order(u16)   — 0=linear, 1=PSP-swizzled (16×8 tile layout)
          +0x08: width(u16)
          +0x0A: height(u16)
          +0x1C: data_offset(u32) — offset from sub-header start to pixel data
```

⚠️ **Important**: The data offset must be read from sub-header `+0x1C`, NOT `+0x00`. The value at `+0x00` is a legacy/reserved field that gives incorrect results.

PSP GPU textures are stored in a **swizzled** layout (16-byte × 8-row tiles) for cache efficiency. The extractor automatically detects and unswizzles based on the `order` field.

Image dimensions are padded to power-of-2 for GPU compatibility (e.g., 480×272 screen → 512×272 texture). Background and CG images are automatically cropped to 480×272.

#### GPDA (Generic Packed Data Archive)
The game uses a custom archive format with the magic bytes `GPDA`. Layout:

```
[0x00]  "GPDA"           (4 bytes, magic)
[0x04]  total_size       (uint32 LE)
[0x08]  unknown          (uint32 LE)
[0x0C]  file_count       (uint32 LE)
[0x10]  entry_table      (file_count × 16 bytes)
            offset       (uint32 LE)
            padding      (uint32 LE)
            size         (uint32 LE)
            name_index   (uint32 LE)
[...]   name_table       (4-byte length prefix + ASCII name, per entry)
```

GPDA archives are often **nested** 2-3 levels deep:
- `first.dat` (GPDA) → `text.dat` (GPDA) → gzipped config files
- `script.dat` (GPDA) → per-chapter `.dat` (GPDA) → inner `.dat` (GPDA) → `.obj.gz` + `.dat.gz`

#### Script .obj Format
Compiled binary scripts. Dialogue records are identified by a `0x00000064` marker:

```
[marker]   0x64 0x00 0x00 0x00
[pad?]     0x00 0x00  (optional 2-byte padding)
[voice_id] int32 LE   (-1 = no voice)
[chara_id] int32 LE   (-1 = narrator)
[text_len] uint32 LE  (character count, NOT byte count)
[text]     text_len × 2 bytes, UTF-16LE encoded
```

⚠️ The `0x64` marker can produce false positives (it's ASCII `'d'`), so post-processing is needed to clean up garbled results.

#### AFS2 / AWB (CRI Middleware Audio Container)
The `voice.awb` file uses AFS2 format:

```
[0x00]  "AFS2"         (4 bytes, magic)
[0x04]  version        (uint32 LE — byte1=ver, byte2=offset_bytes, byte3=id_bytes)
[0x08]  file_count     (uint32 LE)
[0x0C]  alignment      (uint32 LE)
[0x10]  id_table       (file_count × id_bytes)
[...]   offset_table   (file_count+1 × offset_bytes, NO padding after ID table)
```

Audio files are in **HCA** format (CRI High Compression Audio), decoded to WAV via PyCriCodecs.

### Character Voice Mapping

Each voiced character has a range of AWB file IDs defined in `voice_list_dst.txt`:

| ID | Short Name | Character | AWB Range |
|----|-----------|-----------|-----------|
| 0  | YOKO      | 横寺      | 8714–15390 |
| 1  | TUKI      | 月子      | 5261–7046 |
| 2  | AZU       | 梓       | 2592–3818 |
| 3  | TUKU      | つくし    | 3819–5260 |
| 4  | EMI       | エミ      | 7047–8177 |
| 5  | HUKU      | 副部長    | 1436–2591 |
| ...| ...       | ...       | ... |

Voice filename: `{SHORT_NAME}_{index:04d}` where `index = voice_id - range_start`

## Output Statistics (Reference)

Extracted from a complete game ISO:

| Metric | Value |
|--------|-------|
| Chapter files | 305 |
| Total dialogue lines | ~19,900 |
| Voiced lines | ~14,300 |
| WAV voice files | ~14,200 |
| Voice audio size | ~2.1 GB |
| scn text size | ~2.2 MB |
| Background images | 222 |
| Event CG images | 185 |
| Character sprites | 181 (body + expressions) |
| Face sprites | 3,439 |
| Eye-catch images | 54 |

## License

This tool is for personal/research use with legally obtained game copies.
