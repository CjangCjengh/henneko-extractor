#!/usr/bin/env python3
"""
Post-processing script for extracted scn files.
================================================

Fixes issues in the raw extraction output:
  1. VOICE_00000 lines — false positives from the binary 0x64 marker scanner.
     These lines contain embedded multi-line dialogue blocks mixed with binary
     garbage. The script extracts real dialogue and narration from them.
  2. Garbled characters — UTF-16LE decoding artifacts from script control codes
     (e.g. Latin Extended, Greek, Cyrillic characters that don't belong in
     Japanese text).
  3. Character name extraction — some narration lines embed character names
     in the format: 角色名「台詞」. These are split into proper fields.
  4. Empty character names — filled with "null".

Usage:
  python postprocess.py --scn-dir <path_to_scn_folder>
"""

import os
import re
import argparse

# ============================================================
# Garble detection & cleanup
# ============================================================

# Characters that should never appear in Japanese game dialogue
GARBAGE_RE = re.compile(
    r'[\u0000-\u0008\u000b\u000c\u000e-\u001f'  # control chars (excl. \t\n\r)
    r'\u0080-\u024f'      # Latin supplement / extended
    r'\u0250-\u036f'      # IPA / spacing modifiers / combining
    r'\u0370-\u03ff'      # Greek
    r'\u0400-\u04ff'      # Cyrillic
    r'\u0500-\u06ff'      # Cyrillic supp / Armenian / Hebrew / Arabic
    r'\u0900-\u097f'      # Devanagari
    r'\u0e00-\u0eff'      # Thai
    r'\u1200-\u137f'      # Ethiopic
    r'\ufffd-\uffff'      # specials
    r']'
)

# Known character names that may appear embedded in narration
DEFAULT_CHARA_NAMES = [
    '横寺', '月子', '梓', 'つくし', 'エミ', '副部長',
    'モリイ', 'モリヤ', 'ポン太', '梓母', '横寺母',
    'シスター', '会計係', '陸上部員', '女子生徒', '店員',
    '先生', 'オーナー', '猫神', '設備検査係１', '編集長',
    '実行委員長', '校長', '小豆母',
]
# Sort by length descending for greedy matching
_CHARA_SORTED = sorted(DEFAULT_CHARA_NAMES, key=len, reverse=True)
_CHARA_PAT = '|'.join(re.escape(n) for n in _CHARA_SORTED)
EMBED_RE = re.compile(r'(' + _CHARA_PAT + r')「([^」]+)」')


def has_garble(text):
    return bool(GARBAGE_RE.search(text))


def strip_garbage(text):
    return GARBAGE_RE.sub('', text)


def is_valid_jp(text, min_chars=2):
    if len(text) < min_chars:
        return False
    return sum(1 for c in text if
               (0x3040 <= ord(c) <= 0x30FF) or
               (0x4E00 <= ord(c) <= 0x9FFF) or
               (0xFF01 <= ord(c) <= 0xFF5E) or
               c in 'ー〜…、。！？「」『』（）〈〉【】') >= min_chars


def clean_narration(text):
    """Remove garbage and control-code residuals from narration text."""
    clean = strip_garbage(text)
    # Remove engine control prefixes (Pd, @d, `d, 0d, pd + short suffix)
    clean = re.sub(r'[P@`0p]d[)(#$%&\d ]{0,5}', '', clean)
    # Remove leading/trailing ASCII junk
    clean = re.sub(r'^[a-zA-Z0-9\s\-=<>+#$%^&*()_\[\]{};:\'",./\\|~`!@]+', '', clean)
    clean = re.sub(r'[a-zA-Z0-9\s\-=<>+#$%^&*()_\[\]{};:\'",./\\|~`!@]+$', '', clean)
    return clean.strip()


# ============================================================
# Line processors
# ============================================================

def extract_embedded(text):
    """
    Extract embedded dialogue and narration segments from a messy text block.
    Returns: [(type, chara_name, content), ...]
    """
    results = []
    clean = strip_garbage(text)
    matches = list(EMBED_RE.finditer(clean))

    if not matches:
        narr = clean_narration(text)
        if is_valid_jp(narr):
            results.append(('narration', '', narr))
        return results

    # Before first match
    before = clean_narration(clean[:matches[0].start()])
    if is_valid_jp(before):
        results.append(('narration', '', before))

    for i, m in enumerate(matches):
        dialog = strip_garbage(m.group(2).strip())
        dialog = re.sub(r'[P@`0p]d', '', dialog).strip()
        if dialog:
            results.append(('dialog', m.group(1), dialog))
        # Between matches
        if i + 1 < len(matches):
            between = clean_narration(clean[m.end():matches[i+1].start()])
            if is_valid_jp(between):
                results.append(('narration', '', between))

    # After last match
    after = clean_narration(clean[matches[-1].end():])
    if is_valid_jp(after):
        results.append(('narration', '', after))

    return results


def process_line(vname, cname, text):
    """
    Process a single line. Returns list of fixed lines or None (to delete).
    Each line is (voice_name, chara_name, text).
    """
    # Fix known bad voice IDs
    if vname in ('VOICE_00000', 'VOICE_1048576'):
        vname = 'null'

    is_garbled = has_garble(text) or has_garble(cname)

    if not is_garbled and vname != 'null':
        return [(vname, cname, text)]  # clean voiced line — pass through

    if not is_garbled:
        return [(vname, cname, text)]  # clean narration — pass through

    # Line has garble
    if has_garble(cname):
        cname = strip_garbage(cname).strip()

    if not is_valid_jp(strip_garbage(text)):
        return None  # pure garbage — delete

    items = extract_embedded(text)
    if not items:
        return None

    results = []
    for itype, ichara, icontent in items:
        if itype == 'dialog':
            results.append(('null', ichara, icontent))
        else:
            results.append(('null', '', icontent))

    # Preserve original voice tag on first result if it was a real voice line
    if vname != 'null' and results:
        results[0] = (vname, results[0][1] or cname, results[0][2])

    return results or None


def fix_chara_fields(lines):
    """
    Second pass: fix character name fields.
    - Extract character names embedded in narration text: 角色名「内容」 → split
    - Set empty character fields to 'null'
    """
    fixed = []
    for line in lines:
        parts = line.split('|', 2)
        if len(parts) < 3:
            fixed.append(line)
            continue

        vname, cname, text = parts

        if cname == '':
            # Check if text starts with 角色名「...」
            m = re.match(r'^(' + _CHARA_PAT + r')「(.+)」$', text)
            if m:
                cname = m.group(1)
                text = m.group(2)
            else:
                cname = 'null'

        # Remove outer 「」 from voiced dialogue (spoken lines)
        if cname not in ('', 'null') and text.startswith('「') and text.endswith('」'):
            text = text[1:-1]

        fixed.append(f"{vname}|{cname}|{text}")
    return fixed


# ============================================================
# File-level processing
# ============================================================

def fix_file(filepath):
    """Apply all fixes to a single scn .txt file. Returns True if modified."""
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    # Pass 1: fix garble and VOICE_00000
    new_lines = []
    changed = False
    for line in lines:
        line = line.rstrip('\n')
        if not line.strip():
            continue
        parts = line.split('|', 2)
        if len(parts) < 3:
            if has_garble(line) or not is_valid_jp(line):
                changed = True
                continue
            new_lines.append(line)
            continue

        result = process_line(*parts)
        if result is None:
            changed = True
            continue
        for rv, rc, rt in result:
            nl = f"{rv}|{rc}|{rt}"
            new_lines.append(nl)
            if nl != line:
                changed = True

    # Pass 2: fix character name fields
    final_lines = fix_chara_fields(new_lines)
    if final_lines != new_lines:
        changed = True

    if changed:
        with open(filepath, 'w', encoding='utf-8') as f:
            for l in final_lines:
                f.write(l + '\n')
    return changed


def main():
    parser = argparse.ArgumentParser(description="Post-process extracted scn files")
    parser.add_argument('--scn-dir', required=True, help='Path to scn/ folder')
    args = parser.parse_args()

    scn_dir = args.scn_dir
    if not os.path.isdir(scn_dir):
        print(f"Error: directory not found: {scn_dir}")
        return

    total = fixed = 0
    for fname in sorted(os.listdir(scn_dir)):
        if not fname.endswith('.txt'):
            continue
        total += 1
        if fix_file(os.path.join(scn_dir, fname)):
            fixed += 1

    print(f"Post-processing complete: {fixed}/{total} files modified")

    # Verify
    garble_remaining = total_lines = 0
    for fname in sorted(os.listdir(scn_dir)):
        if not fname.endswith('.txt'):
            continue
        with open(os.path.join(scn_dir, fname), 'r', encoding='utf-8') as f:
            for line in f:
                total_lines += 1
                if has_garble(line.rstrip('\n')):
                    garble_remaining += 1

    print(f"  Total lines: {total_lines}")
    print(f"  Remaining garbled lines: {garble_remaining}")


if __name__ == '__main__':
    main()
