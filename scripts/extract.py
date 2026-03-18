#!/usr/bin/env python3
"""
Henneko PSP Game — Dialogue & Voice Extractor
==============================================

Extracts all dialogue lines (voice_name, character_name, text) and voice audio
files from the game ISO:
  「変態王子と笑わない猫。(Hentai Ouji to Warawanai Neko)」 PSP

Workflow:
  1. Open ISO via pycdlib (no mounting needed)
  2. Parse GPDA archives to reach script data (first.dat → text.dat, RES.DAT → script.dat)
  3. Extract character name table (Name.csv) and voice range table (voice_list_dst.txt)
  4. Parse each chapter's binary .obj scripts for dialogue records
  5. Extract voice.awb (CRI AFS2 container with HCA audio), decode HCA → WAV
  6. Output: scn/ folder with per-chapter .txt, voice/ folder with .wav files

Usage:
  python extract.py --iso <path_to_iso> --output <output_dir> [--keep-iso] [--keep-hca]

Dependencies:
  pip install pycdlib tqdm PyCriCodecs
  (PyCriCodecs: pip install git+https://github.com/Youjose/PyCriCodecs.git)
"""

import struct
import gzip
import os
import sys
import re
import argparse
from tqdm import tqdm
import pycdlib

# HCA decoding is optional — if not available, raw HCA files are kept
try:
    from PyCriCodecs import HCA
    HCA_AVAILABLE = True
except ImportError:
    HCA_AVAILABLE = False

# ============================================================
# Game-specific configuration
# ============================================================

# Character ID → short name prefix (used in voice filenames)
CHARA_SHORT_NAMES = {
    0:  "YOKO",     # 横寺
    1:  "TUKI",     # 月子
    2:  "AZU",      # 梓
    3:  "TUKU",     # つくし
    4:  "EMI",      # エミ
    5:  "HUKU",     # 副部長
    6:  "MORI",     # モリイ
    7:  "MORIYA",   # モリヤ
    8:  "PONTA",    # ポン太
    9:  "AZUMOM",   # 梓母
    10: "YOKOMOM",  # 横寺母
    11: "NEKO",     # 猫神
    12: "CMDIR",    # ＣＭディレクター
    13: "CMNAR",    # ＣＭナレーション
    14: "ANNCE",    # アナウンス
    15: "OWNER",    # オーナー
}

# ISO internal paths
PATH_FIRST_DAT = "/PSP_GAME/USRDIR/first.dat"
PATH_RES_DAT   = "/PSP_GAME/INSDIR/RES.DAT"
PATH_VOICE_AWB = "/PSP_GAME/USRDIR/voice.awb"


# ============================================================
# GPDA Archive Parser
# ============================================================

def parse_gpda(data):
    """
    Parse a GPDA (Generic Packed Data Archive) container.
    Returns list of (filename, file_data) tuples, or None if not GPDA.

    GPDA layout:
      [0x00] 4B  magic "GPDA"
      [0x04] 4B  total_size
      [0x08] 4B  unknown
      [0x0C] 4B  file_count
      [0x10] 16B * file_count  entry table (offset, pad, size, name_offset)
      [...]  name table: 4B name_len + name_len bytes ASCII per entry
    """
    if len(data) < 16 or data[:4] != b'GPDA':
        return None
    total_size, unk, file_count = struct.unpack_from('<III', data, 4)
    if file_count == 0 or file_count > 100000:
        return None

    entries = []
    for i in range(file_count):
        off = 16 + i * 16
        if off + 16 > len(data):
            return None
        fo, pad, fs, no = struct.unpack_from('<IIII', data, off)
        entries.append((fo, fs))

    # Parse name table
    name_area = 16 + file_count * 16
    offset = name_area
    names = []
    for i in range(file_count):
        if offset + 4 > len(data):
            names.append(f'file_{i}')
            continue
        name_len = struct.unpack_from('<I', data, offset)[0]
        offset += 4
        if offset + name_len > len(data):
            names.append(f'file_{i}')
            continue
        name = data[offset:offset + name_len].decode('ascii', errors='replace').strip()
        names.append(name)
        offset += name_len

    result = []
    for i in range(file_count):
        fo, fs = entries[i]
        if fo + fs <= len(data):
            result.append((names[i], data[fo:fo + fs]))
        else:
            result.append((names[i], b''))
    return result


def decompress_if_gzip(data):
    """Decompress gzip data if applicable."""
    if len(data) >= 2 and data[:2] == b'\x1f\x8b':
        return gzip.decompress(data)
    return data


# ============================================================
# Metadata Parsers (Name.csv, voice_list_dst.txt)
# ============================================================

def parse_name_csv(data):
    """
    Parse Name.csv → {chara_id: display_name}.
    Format: ON,<id>,<internal_name>,<display_name>
    """
    text = data.decode('shift_jis', errors='replace')
    names = {}
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('ON,'):
            parts = line.split(',')
            if len(parts) >= 4:
                try:
                    names[int(parts[1])] = parts[3]
                except ValueError:
                    continue
    return names


def parse_voice_list(data):
    """
    Parse voice_list_dst.txt → {chara_id: (start_voice_id, end_voice_id)}.
    Format: ON,<chara_id>,<start>,<end>,<count>,...
    """
    text = data.decode('shift_jis', errors='replace')
    voice_ranges = {}
    for line in text.split('\n'):
        line = line.strip()
        if line.startswith('ON,'):
            parts = line.split(',')
            if len(parts) >= 5:
                try:
                    chara_id = int(parts[1])
                    start_id = int(parts[2])
                    end_id = int(parts[3])
                    voice_ranges[chara_id] = (start_id, end_id)
                except ValueError:
                    continue
    return voice_ranges


# ============================================================
# Binary Script Parser (.obj files)
# ============================================================

def parse_obj_script(obj_data):
    """
    Parse a compiled .obj script file for dialogue records.

    Each dialogue record is marked by a 0x00000064 tag followed by:
      [optional 00 00 padding]
      voice_id  (int32)   — AWB file index, -1 if no voice
      chara_id  (int32)   — character table index, -1 if narrator
      text_len  (uint32)  — number of UTF-16LE characters
      text      (text_len * 2 bytes, UTF-16LE)

    Returns: [(voice_id, chara_id, text_str), ...]
    """
    lines = []
    pos = 0
    length = len(obj_data)

    while pos < length - 18:
        # Search for 0x00000064 marker
        if (obj_data[pos] != 0x64 or obj_data[pos+1] != 0 or
                obj_data[pos+2] != 0 or obj_data[pos+3] != 0):
            pos += 1
            continue

        nxt = pos + 4
        # Skip optional 00 00 padding
        if nxt + 2 <= length and obj_data[nxt] == 0 and obj_data[nxt+1] == 0:
            nxt += 2

        if nxt + 12 > length:
            pos += 1
            continue

        voice_id = struct.unpack_from('<i', obj_data, nxt)[0]
        chara_id = struct.unpack_from('<i', obj_data, nxt + 4)[0]
        text_len = struct.unpack_from('<I', obj_data, nxt + 8)[0]

        text_start = nxt + 12
        text_byte_len = text_len * 2

        if text_byte_len <= 0 or text_byte_len > 10000 or text_start + text_byte_len > length:
            pos += 1
            continue

        try:
            text = obj_data[text_start:text_start + text_byte_len].decode('utf-16-le', errors='replace')
            text = text.rstrip('\x00')
            # Validate: must contain Japanese characters
            has_jp = any(
                (0x3040 <= ord(c) <= 0x30FF) or
                (0x4E00 <= ord(c) <= 0x9FFF) or
                (0xFF01 <= ord(c) <= 0xFF5E)
                for c in text[:50]
            )
            if has_jp and len(text) > 1:
                lines.append((voice_id, chara_id, text))
                pos = text_start + text_byte_len
                continue
        except Exception:
            pass

        pos += 1

    return lines


# ============================================================
# High-level extraction from ISO
# ============================================================

def extract_text_data(iso):
    """Extract metadata files from first.dat → text.dat."""
    print("[1/6] Extracting metadata (Name.csv, voice_list_dst.txt) ...")

    with iso.open_file_from_iso(iso_path=PATH_FIRST_DAT) as fp:
        first_data = fp.read()

    first_files = parse_gpda(first_data)
    text_data = None
    for name, data in first_files:
        if name == 'text.dat':
            text_data = data
            break
    if not text_data:
        raise RuntimeError("text.dat not found in first.dat")

    text_files = parse_gpda(text_data)
    result = {}
    for name, data in text_files:
        result[name] = decompress_if_gzip(data)
    return result


def extract_script_data(iso):
    """Extract script.dat from RES.DAT."""
    print("[2/6] Extracting script archive from RES.DAT ...")

    with iso.open_file_from_iso(iso_path=PATH_RES_DAT) as fp:
        header = fp.read(4096)

    total_size, unk, file_count = struct.unpack_from('<III', header, 4)
    entries = []
    for i in range(file_count):
        off = 16 + i * 16
        fo, pad, fs, no = struct.unpack_from('<IIII', header, off)
        entries.append((fo, fs))

    name_area = 16 + file_count * 16
    offset = name_area
    names = []
    for i in range(file_count):
        name_len = struct.unpack_from('<I', header, offset)[0]
        offset += 4
        name = header[offset:offset + name_len].decode('ascii', errors='replace').strip()
        names.append(name)
        offset += name_len

    script_idx = names.index('script.dat')
    script_offset, script_size = entries[script_idx]

    with iso.open_file_from_iso(iso_path=PATH_RES_DAT) as fp:
        fp.seek(script_offset)
        return fp.read(script_size)


def parse_all_scripts(script_data, chara_names, voice_ranges):
    """
    Parse all chapter scripts.
    Returns: {chapter_name: [(voice_name, chara_name, text), ...]}
    """
    print("[3/6] Parsing chapter scripts ...")
    chapters = parse_gpda(script_data)
    if not chapters:
        raise RuntimeError("Cannot parse script.dat")

    all_results = {}
    for chapter_name, chapter_data in tqdm(chapters, desc="  Chapters"):
        ch_name = chapter_name.replace('.dat', '')
        try:
            lines = _parse_chapter(chapter_data, chara_names, voice_ranges)
            if lines:
                all_results[ch_name] = lines
        except Exception as e:
            print(f"  Warning: failed to parse {ch_name}: {e}")
    return all_results


def _parse_chapter(chapter_data, chara_names, voice_ranges):
    """Parse a single chapter (3-layer nested GPDA → .obj.gz)."""
    lines = []
    gpda1 = parse_gpda(chapter_data)
    if not gpda1:
        return lines

    for _, data1 in gpda1:
        gpda2 = parse_gpda(data1)
        if gpda2:
            for name2, data2 in gpda2:
                if name2.endswith('.obj.gz') or name2.endswith('.obj'):
                    obj = decompress_if_gzip(data2)
                    for vid, cid, text in parse_obj_script(obj):
                        vn = _resolve_voice_name(vid, cid, voice_ranges)
                        cn = _resolve_chara_name(cid, chara_names)
                        ct = _clean_text(text, cn)
                        if ct:
                            lines.append((vn, cn, ct))
        else:
            obj = decompress_if_gzip(data1)
            if len(obj) > 100:
                for vid, cid, text in parse_obj_script(obj):
                    vn = _resolve_voice_name(vid, cid, voice_ranges)
                    cn = _resolve_chara_name(cid, chara_names)
                    ct = _clean_text(text, cn)
                    if ct:
                        lines.append((vn, cn, ct))
    return lines


def _resolve_voice_name(voice_id, chara_id, voice_ranges):
    """Map (voice_id, chara_id) → human-readable voice filename."""
    if voice_id < 0 or voice_id == 0xFFFFFFFF:
        return "null"

    # Try chara_id first
    if chara_id in voice_ranges:
        short = CHARA_SHORT_NAMES.get(chara_id, f"CH{chara_id:02d}")
        start, end = voice_ranges[chara_id]
        idx = voice_id - start
        if 0 <= idx <= (end - start):
            return f"{short}_{idx:04d}"

    # Fallback: reverse-lookup by voice_id
    for cid, (start, end) in voice_ranges.items():
        if start <= voice_id <= end:
            short = CHARA_SHORT_NAMES.get(cid, f"CH{cid:02d}")
            return f"{short}_{voice_id - start:04d}"

    return f"VOICE_{voice_id:05d}"


def _resolve_chara_name(chara_id, chara_names):
    if chara_id < 0 or chara_id == 0xFFFFFFFF or chara_id >= 0x7FFFFFFF:
        return ""
    return chara_names.get(chara_id, f"Character_{chara_id}")


def _clean_text(text, chara_name):
    """Strip character name prefix and surrounding brackets from dialogue text."""
    text = text.replace('\x00', '').strip()
    if not text:
        return ""

    # Remove character name prefix: 角色名「...」 → 「...」
    if chara_name and text.startswith(chara_name):
        text = text[len(chara_name):]

    # Remove outer 「」
    if text.startswith('「') and text.endswith('」'):
        text = text[1:-1]

    text = text.strip()
    if len(text) < 1:
        return ""

    # Filter non-Japanese garbage
    jp = sum(1 for c in text if
             (0x3040 <= ord(c) <= 0x30FF) or
             (0x4E00 <= ord(c) <= 0x9FFF) or
             (0xFF01 <= ord(c) <= 0xFF5E) or
             c in 'ー〜…、。！？「」『』（）〈〉【】')
    if jp < len(text) * 0.2 and len(text) > 5:
        return ""
    return text


# ============================================================
# AFS2 / AWB Voice Extraction
# ============================================================

def parse_afs2_offsets(header_data):
    """
    Parse an AFS2 (AWB) file header.
    Returns: {file_id: (byte_offset, byte_size)}

    AFS2 header layout:
      [0x00] 4B  magic "AFS2"
      [0x04] 4B  version (byte1=ver, byte2=offset_bytes, byte3=id_bytes)
      [0x08] 4B  file_count
      [0x0C] 4B  alignment
      [0x10] id_table:  file_count * id_bytes
      [...]  offset_table: (file_count+1) * offset_bytes (no alignment padding)
    """
    if header_data[:4] != b'AFS2':
        raise RuntimeError("Not an AFS2 file")

    version   = struct.unpack_from('<I', header_data, 4)[0]
    file_count = struct.unpack_from('<I', header_data, 8)[0]
    alignment  = struct.unpack_from('<I', header_data, 12)[0]

    id_bytes     = (version >> 16) & 0xFF  # typically 2
    offset_bytes = (version >> 8)  & 0xFF  # typically 4

    # Read ID table
    ids = []
    for i in range(file_count):
        pos = 16 + i * id_bytes
        if id_bytes == 2:
            ids.append(struct.unpack_from('<H', header_data, pos)[0])
        else:
            ids.append(struct.unpack_from('<I', header_data, pos)[0])

    # Offset table starts immediately after ID table (NO alignment padding)
    ot_start = 16 + file_count * id_bytes
    offsets = []
    for i in range(file_count + 1):
        pos = ot_start + i * offset_bytes
        if offset_bytes == 4:
            offsets.append(struct.unpack_from('<I', header_data, pos)[0])
        else:
            offsets.append(struct.unpack_from('<H', header_data, pos)[0])

    # Build file map
    file_map = {}
    for i in range(file_count):
        start = (offsets[i] + alignment - 1) & ~(alignment - 1)
        end = offsets[i + 1]
        file_map[ids[i]] = (start, end - start)

    return file_map


def extract_awb_from_iso(iso, output_path):
    """Extract voice.awb from ISO to a local file for faster random access."""
    print("[5/6] Extracting voice.awb from ISO ...")

    with iso.open_file_from_iso(iso_path=PATH_VOICE_AWB) as fp:
        fp.seek(0, 2)
        total_size = fp.tell()
        fp.seek(0)

        with open(output_path, 'wb') as out:
            chunk = 1024 * 1024
            written = 0
            with tqdm(total=total_size, unit='B', unit_scale=True, desc="  AWB") as pbar:
                while written < total_size:
                    buf = fp.read(min(chunk, total_size - written))
                    if not buf:
                        break
                    out.write(buf)
                    written += len(buf)
                    pbar.update(len(buf))

    print(f"  voice.awb: {total_size / 1024 / 1024:.1f} MB")


def extract_voices(awb_path, needed, output_dir, keep_hca=False):
    """
    Extract and decode voice files from a local AWB.
    needed: {awb_file_id: desired_filename_without_ext}
    """
    print("[6/6] Extracting & decoding voice files ...")

    with open(awb_path, 'rb') as f:
        header = f.read(300000)

    file_map = parse_afs2_offsets(header)
    print(f"  AWB contains {len(file_map)} audio files")

    os.makedirs(output_dir, exist_ok=True)

    to_extract = {}
    for vid, vname in needed.items():
        if vid in file_map:
            to_extract[vid] = (vname, file_map[vid])

    print(f"  Extracting {len(to_extract)} / {len(needed)} voice files")

    # Sort by offset for sequential I/O
    sorted_items = sorted(to_extract.items(), key=lambda x: x[1][1][0])

    ok = fail = 0
    with open(awb_path, 'rb') as f:
        for _, (vname, (start, size)) in tqdm(sorted_items, desc="  Voices"):
            try:
                f.seek(start)
                raw = f.read(size)

                if len(raw) < 4:
                    fail += 1
                    continue

                if HCA_AVAILABLE and not keep_hca and raw[:3] == b'HCA':
                    hca = HCA(raw)
                    wav = hca.decode()
                    with open(os.path.join(output_dir, f"{vname}.wav"), 'wb') as out:
                        out.write(wav)
                else:
                    # Save raw (HCA or unknown format)
                    ext = 'hca' if raw[:3] == b'HCA' else 'bin'
                    with open(os.path.join(output_dir, f"{vname}.{ext}"), 'wb') as out:
                        out.write(raw)
                ok += 1
            except Exception:
                fail += 1

    print(f"  Done. success={ok}, failed={fail}")


# ============================================================
# Output Writers
# ============================================================

def write_scn(all_lines, output_dir):
    """Write per-chapter .txt files in scn/ folder."""
    print("[4/6] Writing dialogue scripts ...")
    os.makedirs(output_dir, exist_ok=True)

    total = 0
    for ch, lines in sorted(all_lines.items()):
        with open(os.path.join(output_dir, f"{ch}.txt"), 'w', encoding='utf-8') as f:
            for vn, cn, tx in lines:
                f.write(f"{vn}|{cn}|{tx}\n")
                total += 1

    print(f"  {len(all_lines)} chapter files, {total} dialogue lines total")


# ============================================================
# Main
# ============================================================

def main():
    parser = argparse.ArgumentParser(
        description="Extract dialogue & voice from Henneko PSP ISO"
    )
    parser.add_argument('--iso', required=True, help='Path to game ISO file')
    parser.add_argument('--output', default='./output', help='Output directory (default: ./output)')
    parser.add_argument('--keep-iso', action='store_true', help='Do not delete the ISO after extraction')
    parser.add_argument('--keep-hca', action='store_true', help='Keep raw HCA files instead of decoding to WAV')
    args = parser.parse_args()

    iso_path = args.iso
    output_dir = args.output
    voice_dir = os.path.join(output_dir, "voice")
    scn_dir = os.path.join(output_dir, "scn")
    temp_awb = os.path.join(output_dir, "_voice_temp.awb")

    print("=" * 60)
    print("Henneko PSP — Dialogue & Voice Extractor")
    print("=" * 60)

    if not os.path.exists(iso_path):
        print(f"Error: ISO not found: {iso_path}")
        sys.exit(1)

    if not HCA_AVAILABLE and not args.keep_hca:
        print("Warning: PyCriCodecs not installed — HCA files will NOT be decoded to WAV.")
        print("         Install it: pip install git+https://github.com/Youjose/PyCriCodecs.git")
        print("         Or use --keep-hca to save raw HCA files.\n")

    iso = pycdlib.PyCdlib()
    iso.open(iso_path)

    try:
        # Step 1: Metadata
        text_data = extract_text_data(iso)
        chara_names = parse_name_csv(text_data['Name.csv'])
        voice_ranges = parse_voice_list(text_data['voice_list_dst.txt'])

        print(f"  Characters: {len(chara_names)}")
        print(f"  Voiced characters: {len(voice_ranges)}")

        # Step 2-3: Scripts
        script_data = extract_script_data(iso)
        all_lines = parse_all_scripts(script_data, chara_names, voice_ranges)

        total = sum(len(l) for l in all_lines.values())
        voiced = sum(1 for l in all_lines.values() for v, _, _ in l if v != "null")
        print(f"\n  Total: {total} lines, {voiced} voiced")

        # Step 4: Write scn
        write_scn(all_lines, scn_dir)

        # Step 4.5: Collect needed voice IDs
        needed = {}
        for ch_lines in all_lines.values():
            for vn, cn, tx in ch_lines:
                if vn == "null":
                    continue
                if vn.startswith('VOICE_'):
                    needed[int(vn[6:])] = vn
                else:
                    parts = vn.rsplit('_', 1)
                    if len(parts) == 2:
                        short, idx_s = parts
                        for cid, sn in CHARA_SHORT_NAMES.items():
                            if sn == short and cid in voice_ranges:
                                needed[voice_ranges[cid][0] + int(idx_s)] = vn
                                break

        print(f"\n  Voice files to extract: {len(needed)}")

        # Step 5: Extract AWB
        extract_awb_from_iso(iso, temp_awb)

    finally:
        iso.close()

    # Step 6: Decode voices
    extract_voices(temp_awb, needed, voice_dir, keep_hca=args.keep_hca)

    # Cleanup
    if os.path.exists(temp_awb):
        os.remove(temp_awb)

    if not args.keep_iso and os.path.exists(iso_path):
        os.remove(iso_path)
        print(f"\n  ISO deleted: {iso_path}")

    print("\n" + "=" * 60)
    print("Extraction complete!")
    print(f"  Dialogue: {scn_dir}/")
    print(f"  Voice:    {voice_dir}/")
    print("=" * 60)


if __name__ == '__main__':
    main()
