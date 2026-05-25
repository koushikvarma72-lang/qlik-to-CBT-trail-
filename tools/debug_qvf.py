# -*- coding: utf-8 -*-
"""
QVF Debug Tool -- dumps everything readable from a QVF file.
Usage: python debug_qvf.py <path_to_qvf>
"""
import io
import json
import os
import re
import sys
import zipfile

# Force UTF-8 output on Windows so we don't crash on special chars
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

SEP = "=" * 70
DIV = "-" * 70


def run(filepath):
    print(f"\n{SEP}")
    print(f"FILE : {os.path.basename(filepath)}")
    print(f"SIZE : {os.path.getsize(filepath):,} bytes")
    print(f"{SEP}\n")

    # ── 1. ZIP-based QVF ─────────────────────────────────────────────────────
    if zipfile.is_zipfile(filepath):
        print("FORMAT: ZIP-based QVF\n")
        with zipfile.ZipFile(filepath, 'r') as zf:
            names = zf.namelist()
            print(f"ZIP CONTENTS ({len(names)} files)")
            print(DIV)
            for name in names:
                info = zf.getinfo(name)
                print(f"  {name:60s} {info.file_size:>10,} bytes")

            # Script file
            script_file = next((f for f in names if f.lower().endswith('.qvs')), None)
            if script_file:
                print(f"\nSCRIPT: {script_file}")
                print(DIV)
                script = zf.read(script_file).decode('utf-8', errors='replace')
                print(script)
            else:
                print("\nWARN: No .qvs script file found in ZIP")

            # JSON files
            for name in names:
                if name.lower().endswith('.json'):
                    print(f"\nJSON: {name}")
                    print(DIV)
                    try:
                        data = json.loads(zf.read(name).decode('utf-8', errors='replace'))
                        print(json.dumps(data, indent=2)[:8000])
                    except Exception as e:
                        print(f"  (could not parse: {e})")

            # Other readable text files
            for name in names:
                if not name.lower().endswith(('.qvs', '.json')):
                    try:
                        raw = zf.read(name)
                        text = raw.decode('utf-8', errors='replace')
                        printable = sum(1 for c in text if c.isprintable() or c in '\n\r\t')
                        if len(text) > 0 and printable / len(text) > 0.7:
                            print(f"\nTEXT FILE: {name}")
                            print(DIV)
                            print(text[:3000])
                    except Exception:
                        pass
        return

    # ── 2. Binary QVF ────────────────────────────────────────────────────────
    print("FORMAT: Binary QVF (not a ZIP)\n")
    with open(filepath, 'rb') as f:
        raw = f.read()

    header_hex = raw[:16].hex()
    magic_label = "(known Qlik binary header)" if raw[:4] == b'\xff\xff\x01\x00' else "(unknown header)"
    print(f"HEADER (first 16 bytes): {header_hex}")
    print(f"MAGIC : {raw[:4].hex()} {magic_label}\n")

    text_utf8 = raw.decode('utf-8', errors='replace')

    # ── 2a. All readable text blocks ─────────────────────────────────────────
    print(f"ALL READABLE TEXT BLOCKS (>80 printable chars)")
    print(DIV)
    blocks = []
    current = []
    for byte in raw:
        if 32 <= byte <= 126 or byte in (9, 10, 13):
            current.append(chr(byte))
        else:
            if len(current) > 80:
                blocks.append(''.join(current))
            current = []
    if len(current) > 80:
        blocks.append(''.join(current))

    print(f"Found {len(blocks)} text blocks\n")
    for i, block in enumerate(blocks):
        print(f"  -- Block {i+1} ({len(block)} chars) --")
        print(block[:3000])
        print()

    # ── 2b. Qlik keyword scan ─────────────────────────────────────────────────
    print(f"\nQLIK SCRIPT KEYWORD SCAN")
    print(DIV)
    keywords = [
        'LOAD', 'SELECT', 'SET ', 'LET ', 'STORE ', 'CONNECT', 'RESIDENT',
        'FROM', 'WHERE', 'GROUP BY', 'ORDER BY', 'JOIN', 'INLINE',
        'DIRECTORY', 'CONCATENATE', 'DROP TABLE', 'MAPPING', 'NOCONCATENATE',
    ]
    upper = text_utf8.upper()
    for kw in keywords:
        count = upper.count(kw.upper())
        if count:
            print(f"  {kw:25s} {count:4d} occurrence(s)")

    # ── 2c. Table definitions ─────────────────────────────────────────────────
    print(f"\nTABLE DEFINITIONS")
    print(DIV)
    table_matches = re.findall(
        r'(?:\[([^\]]+)\]|([A-Za-z_][A-Za-z0-9_\s]{0,40})):\s*(?:LOAD|SELECT)',
        text_utf8, re.IGNORECASE
    )
    tables = [m[0] or m[1] for m in table_matches]
    if tables:
        for t in tables:
            print(f"  TABLE: {t.strip()}")
    else:
        print("  None found")

    # ── 2d. SET/LET variables ─────────────────────────────────────────────────
    print(f"\nSET/LET VARIABLES")
    print(DIV)
    vars_found = re.findall(
        r'(?:SET|LET)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*([^\n;]{0,120})',
        text_utf8, re.IGNORECASE
    )
    if vars_found:
        for name, val in vars_found:
            print(f"  {name} = {val.strip()}")
    else:
        print("  None found")

    # ── 2e. Field names near $Field marker ───────────────────────────────────
    print(f"\nFIELD NAMES (near $Field marker)")
    print(DIV)
    field_marker = text_utf8.find('$Field')
    if field_marker >= 0:
        chunk = text_utf8[field_marker:field_marker + 4000]
        tokens = re.findall(r'[A-Za-z][A-Za-z0-9 .()/_\-]{1,60}', chunk)
        blacklist = {
            'field', 'table', 'rows', 'fields', 'fieldno', 'info',
            'fileformat', 'file32', 'filetype', 'format', 'gzjson',
            'usecompression', 'scramblea', 'scrambleb', 'prohibitbinaryload',
            'qvffileversion', 'buildno',
        }
        seen = set()
        for t in tokens:
            v = t.strip()
            if v.lower() not in blacklist and len(v) > 1 and v.lower() not in seen:
                seen.add(v.lower())
                print(f"  {v}")
    else:
        print("  No $Field marker found in file")

    # ── 2f. lib:// data connections ───────────────────────────────────────────
    print(f"\nDATA CONNECTIONS (lib://)")
    print(DIV)
    lib_refs = re.findall(r'lib://[^\s\x00\'"]{3,120}', text_utf8, re.IGNORECASE)
    unique_refs = sorted(set(lib_refs))
    if unique_refs:
        for ref in unique_refs:
            print(f"  {ref}")
    else:
        print("  None found")

    # ── 2g. Binary table labels (label+lib pattern) ───────────────────────────
    print(f"\nBINARY TABLE LABELS (label+lib:// pattern)")
    print(DIV)
    label_matches = re.findall(
        r'([A-Za-z_][A-Za-z0-9_ ]{0,80})\+lib://[^\s\x00]+',
        text_utf8
    )
    if label_matches:
        for lbl in label_matches:
            print(f"  {lbl.strip()}")
    else:
        print("  None found")

    # ── 2h. UTF-16 attempt ────────────────────────────────────────────────────
    print(f"\nUTF-16 DECODE ATTEMPT")
    print(DIV)
    try:
        text16 = raw.decode('utf-16', errors='replace')
        kw_count = sum(text16.upper().count(kw) for kw in ['LOAD', 'SELECT', 'SET '])
        print(f"  UTF-16 chars: {len(text16):,}  |  Qlik keywords found: {kw_count}")
        if kw_count > 0:
            print("  Script appears to be UTF-16 encoded. First 3000 chars:")
            print(DIV)
            print(text16[:3000])
    except Exception as e:
        print(f"  UTF-16 decode failed: {e}")

    # ── 2i. Raw hex dump of first 512 bytes ───────────────────────────────────
    print(f"\nRAW HEX DUMP (first 512 bytes)")
    print(DIV)
    for i in range(0, min(512, len(raw)), 16):
        chunk = raw[i:i+16]
        hex_part = ' '.join(f'{b:02x}' for b in chunk)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in chunk)
        print(f"  {i:04x}  {hex_part:<47s}  {asc_part}")

    print(f"\n{SEP}")
    print("DEBUG COMPLETE")
    print(f"{SEP}\n")


if __name__ == '__main__':
    if len(sys.argv) < 2:
        uploads = os.path.join(os.path.dirname(__file__), 'uploads')
        samples = os.path.join(os.path.dirname(__file__), 'samples')
        qvfs = []
        for folder in [uploads, samples]:
            if os.path.exists(folder):
                for root, dirs, files in os.walk(folder):
                    for f in files:
                        if f.lower().endswith('.qvf'):
                            qvfs.append(os.path.join(root, f))
        if not qvfs:
            print("No QVF files found. Pass a path: python debug_qvf.py file.qvf")
            sys.exit(1)
        filepath = max(qvfs, key=os.path.getmtime)
        print(f"Auto-selected: {filepath}\n")
    else:
        filepath = sys.argv[1]

    run(filepath)
