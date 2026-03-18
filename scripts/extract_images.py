#!/usr/bin/env python3
"""
Henneko PSP Game — Image Extractor (Backgrounds, CGs, Sprites)
==============================================================

Extracts all image assets from the game's RES.DAT archive:
  - Backgrounds (script_bg.dat)       → bg/
  - Event CGs (script_event.dat)      → cg/
  - Character sprites (script_charactor.dat)      → sprite/
  - Face sprites (script_charactor_face.dat)      → face/
  - Eye-catch images (script_eye_catch.dat)       → eyecatch/
  - Character name plates (script_charactor_name.dat) → nameplate/
  - Character items (script_charactor_item.dat)   → item/

All images are decoded from PSP GIM format (MIG.00.1) to PNG.

Usage:
  python extract_images.py --res <path_to_RES.DAT> --output <output_dir>
  python extract_images.py --iso <path_to_ISO>     --output <output_dir>

Dependencies:
  pip install pycdlib tqdm numpy Pillow
"""

import struct
import gzip
import os
import sys
import argparse
import numpy as np
from PIL import Image
from tqdm import tqdm

# Optional: pycdlib for ISO reading
try:
    import pycdlib
    PYCDLIB_AVAILABLE = True
except ImportError:
    PYCDLIB_AVAILABLE = False


# ============================================================
# GPDA Archive Parser (shared with extract.py)
# ============================================================

def parse_gpda(data):
    """
    Parse a GPDA (Generic Packed Data Archive) container.
    Returns list of (filename, file_data) tuples, or None if not GPDA.
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
# PSP GIM (MIG.00.1) Image Decoder
# ============================================================

def _parse_gim_blocks(data, offset, end):
    """
    Recursively parse GIM block tree.

    GIM block header (16 bytes):
      [0x00] type     (uint16) — 0x02=root, 0x03=image, 0x04=pixels, 0x05=palette
      [0x02] unk      (uint16)
      [0x04] size     (uint32) — total block size including header
      [0x08] next_off (uint32)
      [0x0C] data_off (uint32)

    For type 0x04/0x05 (pixel/palette data blocks):
      Sub-header follows block header (variable size, typically 0x40 bytes).
      Sub-header layout:
        +0x00: reserved/legacy data offset (uint32)
        +0x04: pixel_format (uint16) — 0x00=RGB565, 0x01=RGBA5551, 0x02=RGBA4444,
                                        0x03=RGBA8888, 0x04=index4, 0x05=index8
        +0x06: pixel_order (uint16) — 0=linear, 1=PSP-swizzled
        +0x08: width (uint16)
        +0x0A: height (uint16)
        +0x0C: bpp_metadata (uint16)
        +0x0E: pitch_align (uint16)
        +0x10: height_align (uint16)
        +0x12: dim_count (uint16)
        ...
        +0x1C: actual_data_offset (uint32) — CORRECT offset from sub-header start
    """
    blocks = []
    while offset < end - 16:
        btype = struct.unpack_from('<H', data, offset)[0]
        bsize = struct.unpack_from('<I', data, offset + 4)[0]
        if bsize == 0:
            break
        block_end = offset + bsize

        if btype in (0x04, 0x05):
            sub_start = offset + 16
            # Read enough sub-header for all fields we need
            sub_header = data[sub_start:sub_start + 0x40]
            # The CORRECT data offset is at +0x1C in the sub-header
            if len(sub_header) >= 0x20:
                data_off_rel = struct.unpack_from('<I', sub_header, 0x1C)[0]
            else:
                data_off_rel = struct.unpack_from('<I', sub_header, 0x00)[0]
            pixel_start = sub_start + data_off_rel
            pixel_data = data[pixel_start:block_end]
            blocks.append({
                'type': btype,
                'sub_header': sub_header,
                'data': pixel_data,
            })
        elif btype in (0x02, 0x03):
            child = _parse_gim_blocks(data, offset + 16, block_end)
            blocks.extend(child)

        offset = block_end
    return blocks


def _psp_unswizzle(swizzled, width_bytes, height):
    """
    Undo PSP GE texture swizzle (16-byte × 8-row tile layout).

    PSP stores textures in tiles for GPU cache efficiency:
      Tile width  = 16 bytes
      Tile height = 8 rows
    Tiles are arranged left-to-right, top-to-bottom.
    Within each tile, rows are stored sequentially.
    """
    BW, BH = 16, 8
    row_blocks = (width_bytes + BW - 1) // BW
    aligned_w = row_blocks * BW
    out = bytearray(aligned_w * height)

    src_idx = 0
    for by in range(0, height, BH):
        for bx in range(0, aligned_w, BW):
            for row in range(BH):
                y = by + row
                if y >= height:
                    src_idx += BW
                    continue
                dst_start = y * aligned_w + bx
                src_end = src_idx + BW
                if src_end <= len(swizzled):
                    out[dst_start:dst_start + BW] = swizzled[src_idx:src_end]
                src_idx += BW

    return bytes(out[:width_bytes * height])


def _parse_palette(data, fmt, count):
    """
    Parse GIM palette data.
    Supported formats: RGBA8888 (0x03), RGBA4444 (0x02), RGBA5551 (0x01), RGB565 (0x00)
    """
    palette = np.zeros((count, 4), dtype=np.uint8)
    for i in range(count):
        if fmt == 0x03:  # RGBA8888 — 4 bytes per color
            off = i * 4
            if off + 4 <= len(data):
                palette[i] = [data[off], data[off + 1], data[off + 2], data[off + 3]]
        elif fmt == 0x02:  # RGBA4444 — 2 bytes per color
            off = i * 2
            if off + 2 <= len(data):
                v = struct.unpack_from('<H', data, off)[0]
                palette[i] = [
                    ((v >> 0) & 0xF) * 17,
                    ((v >> 4) & 0xF) * 17,
                    ((v >> 8) & 0xF) * 17,
                    ((v >> 12) & 0xF) * 17,
                ]
        elif fmt == 0x01:  # RGBA5551 — 2 bytes per color
            off = i * 2
            if off + 2 <= len(data):
                v = struct.unpack_from('<H', data, off)[0]
                palette[i] = [
                    ((v >> 0) & 0x1F) * 255 // 31,
                    ((v >> 5) & 0x1F) * 255 // 31,
                    ((v >> 10) & 0x1F) * 255 // 31,
                    ((v >> 15) & 0x1) * 255,
                ]
        elif fmt == 0x00:  # RGB565 — 2 bytes per color (no alpha)
            off = i * 2
            if off + 2 <= len(data):
                v = struct.unpack_from('<H', data, off)[0]
                palette[i] = [
                    ((v >> 0) & 0x1F) * 255 // 31,
                    ((v >> 5) & 0x3F) * 255 // 63,
                    ((v >> 11) & 0x1F) * 255 // 31,
                    255,
                ]
    return palette


def decode_gim(data):
    """
    Decode a PSP GIM (MIG.00.1) image to a PIL Image (RGBA).

    Supports:
      - Indexed 4-bit (format 0x04) with palette
      - Indexed 8-bit (format 0x05) with palette
      - Direct RGBA8888 (format 0x03)
      - Direct RGBA4444 (format 0x02)
      - PSP texture swizzle (pixel_order=1)

    Returns: PIL.Image.Image or None on failure.
    """
    if len(data) < 16 or data[:8] != b'MIG.00.1':
        return None

    blocks = _parse_gim_blocks(data, 16, len(data))

    img_block = pal_block = None
    for b in blocks:
        if b['type'] == 0x04:
            img_block = b
        elif b['type'] == 0x05:
            pal_block = b

    if not img_block:
        return None

    sh = img_block['sub_header']
    pixel_fmt = struct.unpack_from('<H', sh, 0x04)[0]
    pixel_order = struct.unpack_from('<H', sh, 0x06)[0]
    width = struct.unpack_from('<H', sh, 0x08)[0]
    height = struct.unpack_from('<H', sh, 0x0A)[0]

    if width == 0 or height == 0 or width > 4096 or height > 4096:
        return None

    # Parse palette
    palette = None
    if pal_block:
        psh = pal_block['sub_header']
        pal_fmt = struct.unpack_from('<H', psh, 0x04)[0]
        pal_count = struct.unpack_from('<H', psh, 0x08)[0]
        palette = _parse_palette(pal_block['data'], pal_fmt, pal_count)

    pix_data = img_block['data']

    # Unswizzle if needed (pixel_order=1 means PSP-swizzled)
    if pixel_fmt == 0x05:  # Indexed 8-bit
        row_bytes = width
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        indices = np.frombuffer(raw[:width * height], dtype=np.uint8).reshape(height, width)
        if palette is not None:
            indices = np.clip(indices, 0, len(palette) - 1)
            return Image.fromarray(palette[indices], 'RGBA')

    elif pixel_fmt == 0x04:  # Indexed 4-bit
        row_bytes = (width + 1) // 2
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        packed = np.frombuffer(raw[:row_bytes * height], dtype=np.uint8).reshape(height, row_bytes)
        indices = np.empty((height, row_bytes * 2), dtype=np.uint8)
        indices[:, 0::2] = packed & 0x0F
        indices[:, 1::2] = (packed >> 4) & 0x0F
        indices = indices[:, :width]
        if palette is not None:
            indices = np.clip(indices, 0, len(palette) - 1)
            return Image.fromarray(palette[indices], 'RGBA')

    elif pixel_fmt == 0x03:  # RGBA8888
        row_bytes = width * 4
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        rgba = np.frombuffer(raw[:width * height * 4], dtype=np.uint8).reshape(height, width, 4)
        return Image.fromarray(rgba, 'RGBA')

    elif pixel_fmt == 0x02:  # RGBA4444
        row_bytes = width * 2
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        raw16 = np.frombuffer(raw[:width * height * 2], dtype=np.uint16).reshape(height, width)
        r = ((raw16 >> 0) & 0xF).astype(np.uint8) * 17
        g = ((raw16 >> 4) & 0xF).astype(np.uint8) * 17
        b = ((raw16 >> 8) & 0xF).astype(np.uint8) * 17
        a = ((raw16 >> 12) & 0xF).astype(np.uint8) * 17
        rgba = np.stack([r, g, b, a], axis=-1)
        return Image.fromarray(rgba, 'RGBA')

    elif pixel_fmt == 0x01:  # RGBA5551
        row_bytes = width * 2
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        raw16 = np.frombuffer(raw[:width * height * 2], dtype=np.uint16).reshape(height, width)
        r = ((raw16 >> 0) & 0x1F).astype(np.uint8) * 255 // 31
        g = ((raw16 >> 5) & 0x1F).astype(np.uint8) * 255 // 31
        b = ((raw16 >> 10) & 0x1F).astype(np.uint8) * 255 // 31
        a = ((raw16 >> 15) & 0x1).astype(np.uint8) * 255
        rgba = np.stack([r, g, b, a], axis=-1)
        return Image.fromarray(rgba, 'RGBA')

    elif pixel_fmt == 0x00:  # RGB565
        row_bytes = width * 2
        raw = _psp_unswizzle(pix_data, row_bytes, height) if pixel_order == 1 else pix_data
        raw16 = np.frombuffer(raw[:width * height * 2], dtype=np.uint16).reshape(height, width)
        r = ((raw16 >> 0) & 0x1F).astype(np.uint8) * 255 // 31
        g = ((raw16 >> 5) & 0x3F).astype(np.uint8) * 255 // 63
        b = ((raw16 >> 11) & 0x1F).astype(np.uint8) * 255 // 31
        a = np.full((height, width), 255, dtype=np.uint8)
        rgba = np.stack([r, g, b, a], axis=-1)
        return Image.fromarray(rgba, 'RGBA')

    return None


# ============================================================
# Image Archive Extraction
# ============================================================

# PSP screen resolution: 480×272
# GPU textures are power-of-2 aligned (512×272 or 512×512)
# Right-side padding (512 → 480) should be cropped for BG/CG images.
PSP_SCREEN_WIDTH = 480
PSP_SCREEN_HEIGHT = 272


def _extract_simple_archive(archive_data, output_dir, crop_to_screen=False, desc="Images"):
    """
    Extract a GPDA archive of gzipped GIM files → PNG.
    Each entry: gzip(GIM) → decode → PNG.
    """
    entries = parse_gpda(archive_data)
    if not entries:
        return 0

    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for name, data in tqdm(entries, desc=f"  {desc}"):
        try:
            raw = decompress_if_gzip(data)
            img = decode_gim(raw)
            if img is None:
                continue
            if img.size == (1, 1):
                continue  # skip 1×1 placeholder images

            if crop_to_screen and img.width > PSP_SCREEN_WIDTH:
                img = img.crop((0, 0, PSP_SCREEN_WIDTH, img.height))

            out_name = name.replace('.gim', '.png').replace('.GIM', '.png')
            if not out_name.endswith('.png'):
                out_name += '.png'
            img.save(os.path.join(output_dir, out_name))
            count += 1
        except Exception as e:
            print(f"  Warning: failed to decode {name}: {e}")

    return count


def _extract_sprite_archive(archive_data, output_dir, desc="Sprites"):
    """
    Extract character sprite archive (nested GPDA → multiple GIM per character).

    Structure per character entry:
      gzip → GPDA(
        body.gim    (512×512 — full body sprite)
        face_00.gim (128×128/128×192 — expression overlays)
        face_01.gim
        ...
      )

    Output: sprite/<CharName>_body.png, sprite/<CharName>_face_00.png, etc.
    """
    entries = parse_gpda(archive_data)
    if not entries:
        return 0

    os.makedirs(output_dir, exist_ok=True)
    count = 0

    for name, data in tqdm(entries, desc=f"  {desc}"):
        try:
            raw = decompress_if_gzip(data)

            # Check if it's a nested GPDA (character with multiple layers)
            if raw[:4] == b'GPDA':
                nested = parse_gpda(raw)
                if not nested:
                    continue
                base = name.replace('.gim', '').replace('.GIM', '')
                body_idx = 0
                face_idx = 0
                for nname, ndata in nested:
                    nraw = decompress_if_gzip(ndata)
                    img = decode_gim(nraw)
                    if img is None or img.size == (1, 1):
                        continue
                    # First large image (≥256px wide) is the body
                    if img.width >= 256 and body_idx == 0:
                        img.save(os.path.join(output_dir, f"{base}_body.png"))
                        body_idx += 1
                    else:
                        img.save(os.path.join(output_dir, f"{base}_face_{face_idx:02d}.png"))
                        face_idx += 1
                    count += 1
            else:
                # Simple GIM file
                img = decode_gim(raw)
                if img and img.size != (1, 1):
                    out_name = name.replace('.gim', '.png').replace('.GIM', '.png')
                    img.save(os.path.join(output_dir, out_name))
                    count += 1
        except Exception as e:
            print(f"  Warning: failed to decode {name}: {e}")

    return count


# ============================================================
# Main
# ============================================================

# RES.DAT archive names → (output_subfolder, extraction_type, crop_flag)
IMAGE_ARCHIVES = {
    'script_bg.dat':              ('bg',        'simple', True),
    'script_event.dat':           ('cg',        'simple', True),
    'script_charactor.dat':       ('sprite',    'sprite', False),
    'script_charactor_face.dat':  ('face',      'simple', False),
    'script_charactor_item.dat':  ('item',      'simple', False),
    'script_charactor_name.dat':  ('nameplate', 'simple', False),
    'script_eye_catch.dat':       ('eyecatch',  'simple', True),
}


def main():
    parser = argparse.ArgumentParser(
        description="Extract images (BG, CG, sprites) from Henneko PSP game"
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--res', help='Path to extracted RES.DAT file')
    group.add_argument('--iso', help='Path to game ISO (will extract RES.DAT automatically)')
    parser.add_argument('--output', default='./output/images', help='Output directory')
    parser.add_argument('--types', nargs='+', default=None,
                        choices=['bg', 'cg', 'sprite', 'face', 'item', 'nameplate', 'eyecatch'],
                        help='Only extract specific image types (default: all)')
    args = parser.parse_args()

    output_dir = args.output

    print("=" * 60)
    print("Henneko PSP — Image Extractor")
    print("=" * 60)

    # Load RES.DAT
    if args.iso:
        if not PYCDLIB_AVAILABLE:
            print("Error: pycdlib is required for ISO reading.")
            print("  pip install pycdlib")
            sys.exit(1)
        print(f"[1/2] Reading RES.DAT from ISO: {args.iso}")
        iso = pycdlib.PyCdlib()
        iso.open(args.iso)
        try:
            with iso.open_file_from_iso(iso_path="/PSP_GAME/INSDIR/RES.DAT") as fp:
                res_data = fp.read()
        finally:
            iso.close()
    else:
        print(f"[1/2] Reading RES.DAT: {args.res}")
        with open(args.res, 'rb') as f:
            res_data = f.read()

    # Parse top-level GPDA
    res_files = parse_gpda(res_data)
    if not res_files:
        print("Error: failed to parse RES.DAT as GPDA archive")
        sys.exit(1)

    res_dict = {name: data for name, data in res_files}
    print(f"  RES.DAT contains {len(res_dict)} entries")

    # Extract images
    print(f"\n[2/2] Extracting images to: {output_dir}")
    total_images = 0

    for arch_name, (subfolder, extract_type, crop) in IMAGE_ARCHIVES.items():
        if args.types and subfolder not in args.types:
            continue

        arch_data = res_dict.get(arch_name)
        if not arch_data:
            print(f"  Skipping {arch_name} (not found in RES.DAT)")
            continue

        sub_dir = os.path.join(output_dir, subfolder)
        if extract_type == 'sprite':
            n = _extract_sprite_archive(arch_data, sub_dir, desc=subfolder)
        else:
            n = _extract_simple_archive(arch_data, sub_dir, crop_to_screen=crop, desc=subfolder)
        total_images += n
        print(f"  {subfolder}: {n} images")

    print(f"\n{'=' * 60}")
    print(f"Extraction complete! {total_images} images saved to {output_dir}/")
    print("=" * 60)


if __name__ == '__main__':
    main()
