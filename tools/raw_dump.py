# -*- coding: utf-8 -*-
"""
RAW DUMP — shows every readable string in the QVF with its exact byte offset.
No interpretation. No assumptions. Just what is literally in the file.
Output goes to raw_dump.txt
"""
import os, re, sys

# Find the uploaded QVF
uploads = os.path.join(os.path.dirname(__file__), 'uploads')
qvfs = []
for root, _, files in os.walk(uploads):
    for f in files:
        if f.lower().endswith('.qvf'):
            qvfs.append(os.path.join(root, f))

if not qvfs:
    print("No QVF in uploads/"); sys.exit(1)

filepath = max(qvfs, key=os.path.getmtime)
print(f"Reading: {os.path.basename(filepath)}")
print(f"Size   : {os.path.getsize(filepath):,} bytes")

with open(filepath, 'rb') as f:
    raw = f.read()

out = open('raw_dump.txt', 'w', encoding='utf-8', errors='replace')

def w(s=''):
    out.write(s + '\n')

w(f"FILE: {os.path.basename(filepath)}")
w(f"SIZE: {os.path.getsize(filepath):,} bytes")
w(f"HEADER (first 32 bytes): {raw[:32].hex()}")
w()

# ── Every readable string >= 3 chars, with offset and the 4 bytes before it ──
w("=" * 80)
w("EVERY READABLE STRING (>= 3 printable chars)")
w("Format:  offset | bytes_before(4) | length | string")
w("=" * 80)

i = 0
count = 0
while i < len(raw):
    if 32 <= raw[i] <= 126 or raw[i] in (9, 10, 13):
        j = i
        while j < len(raw) and (32 <= raw[j] <= 126 or raw[j] in (9, 10, 13)):
            j += 1
        s = raw[i:j].decode('ascii', errors='replace').strip()
        if len(s) >= 3:
            pre4 = raw[max(0, i-4):i].hex()
            w(f"  0x{i:07x}  pre={pre4:8s}  len={len(s):5d}  [{s[:300]}]")
            count += 1
        i = j
    else:
        i += 1

w()
w(f"Total strings found: {count}")
out.close()
print(f"Done. {count} strings written to raw_dump.txt")
