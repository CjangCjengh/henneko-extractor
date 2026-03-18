# Henneko PSP — Dialogue & Voice Extractor

Extracts all dialogue lines and character voice audio from the PSP game:

> **変態王子と笑わない猫。** (Hentai Ouji to Warawanai Neko / "The Hentai Prince and the Stony Cat")

## What This Does

```
Game ISO (.iso)
  ├─ first.dat → text.dat → Name.csv (character table) + voice_list_dst.txt
  ├─ RES.DAT  → script.dat → per-chapter .obj binary scripts (dialogue records)
  └─ voice.awb → AFS2 container → HCA audio files
```

**Output:**

```
output/
  ├─ scn/                    # Dialogue scripts (one .txt per chapter)
  │   ├─ A_K00_prologue.txt
  │   ├─ A_K01_01_01.txt
  │   └─ ...
  └─ voice/                  # Voice audio files (.wav)
      ├─ YOKO_0000.wav
      ├─ TUKI_0000.wav
      └─ ...
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
pip install pycdlib tqdm
pip install git+https://github.com/Youjose/PyCriCodecs.git  # HCA decoder
```

- **Python 3.8+**
- `pycdlib` — ISO reading without mounting
- `tqdm` — progress bars
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
| `--keep-iso` | Don't delete the ISO after extraction |
| `--keep-hca` | Save raw HCA audio instead of decoding to WAV |

The ISO is deleted after extraction by default to save disk space (~1.4 GB).

### Step 2: Post-process dialogue scripts

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
