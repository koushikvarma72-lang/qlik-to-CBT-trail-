# -*- coding: utf-8 -*-
"""
Deep binary inspection of a QVF file.
Prints every readable string >= 4 chars with its byte offset,
so we can see exactly what is stored and in what order.
"""
import io, os, re, sys, zipfile
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')

UPLOADS = os.path.join(os.path.dirname(__file__), 'uploads')
SAMPLES = os.path.join(os.path.dirname(__file__), 'samples')

def find_qvf():
    for folder in [UPLOADS, SAMPLES]:
        if not os.path.exists(folder):
            continue
        for root, _, files in os.walk(folder):
            for f in files:
                if f.lower().endswith('.qvf'):
                    return os.path.join(root, f)
    return None

filepath = sys.argv[1] if len(sys.argv) > 1 else find_qvf()
if not filepath:
    print("No QVF found"); sys.exit(1)

print(f"FILE : {os.path.basename(filepath)}")
print(f"SIZE : {os.path.getsize(filepath):,} bytes\n")

# ── ZIP check ────────────────────────────────────────────────────────────────
if zipfile.is_zipfile(filepath):
    print("FORMAT: ZIP-based QVF")
    with zipfile.ZipFile(filepath) as zf:
        print("Contents:", zf.namelist())
        for name in zf.namelist():
            if name.lower().endswith('.qvs'):
                print(f"\n=== SCRIPT: {name} ===")
                print(zf.read(name).decode('utf-8', errors='replace'))
    sys.exit(0)

print("FORMAT: Binary QVF\n")
with open(filepath, 'rb') as f:
    raw = f.read()

# ── 1. Every printable string >= 4 chars with offset ─────────────────────────
print("=" * 70)
print("ALL STRINGS >= 4 CHARS (offset | length | content)")
print("=" * 70)
i = 0
strings = []
while i < len(raw):
    if 32 <= raw[i] <= 126 or raw[i] in (9, 10, 13):
        j = i
        while j < len(raw) and (32 <= raw[j] <= 126 or raw[j] in (9, 10, 13)):
            j += 1
        s = raw[i:j].decode('ascii', errors='replace').strip()
        if len(s) >= 4:
            strings.append((i, s))
        i = j
    else:
        i += 1

for offset, s in strings:
    # Show the 1-2 bytes BEFORE this string (the length/type bytes)
    pre = raw[max(0, offset-2):offset].hex()
    print(f"  0x{offset:06x} [{len(s):4d}] pre={pre} | {s[:200]}")

# ── 2. Byte immediately before each string that looks like a table name ───────
print("\n" + "=" * 70)
print("TABLE NAME CANDIDATES (strings followed by a Windows path)")
print("=" * 70)
path_re = re.compile(r'([A-Za-z]:\\[^\x00\n\r]{10,200})')
for offset, s in strings:
    if path_re.search(s):
        # The string itself contains a path — find where the path starts
        m = path_re.search(s)
        table_part = s[:m.start()].strip()
        path_part = m.group(1)
        # The byte right before this string block
        pre_byte = raw[offset-1] if offset > 0 else 0
        print(f"  offset=0x{offset:06x}  pre_byte=0x{pre_byte:02x}({chr(pre_byte) if 32<=pre_byte<=126 else '?'})")
        print(f"    RAW_NAME : [{table_part}]")
        print(f"    PATH     : {path_part}")
        print()

# ── 3. $Field section — show raw bytes around it ─────────────────────────────
print("=" * 70)
print("$FIELD SECTION — raw bytes + decoded strings")
print("=" * 70)
marker = b'$Field'
pos = raw.find(marker)
if pos >= 0:
    chunk = raw[pos:pos+6000]
    # Print hex + ascii side by side in 32-byte rows
    for row in range(0, min(len(chunk), 3000), 32):
        seg = chunk[row:row+32]
        hex_part = ' '.join(f'{b:02x}' for b in seg)
        asc_part = ''.join(chr(b) if 32 <= b < 127 else '.' for b in seg)
        print(f"  {pos+row:06x}  {hex_part:<95s}  {asc_part}")
else:
    print("  $Field marker not found")

# ── 4. All unique field-like tokens from $Field section ──────────────────────
print("\n" + "=" * 70)
print("FIELD TOKENS from $Field section (deduplicated)")
print("=" * 70)
if pos >= 0:
    text_chunk = raw[pos:pos+8000].decode('utf-8', errors='replace')
    tokens = re.findall(r'[A-Za-z][A-Za-z0-9 .()\-/_]{1,60}', text_chunk)
    noise = {
        'field','table','rows','fields','fieldno','info','fileformat',
        'file32','filetype','usecompression','scramblea','scrambleb',
        'prohibitbinaryload','qvffileversion','buildno','format','gzjson',
        'usebinary','istemporary','contenthash','database','qvd','minstring',
        'language','jan','feb','mar','apr','may','jun','jul','aug','sep',
        'oct','nov','dec','mon','tue','wed','thu','fri','sat','sun',
        'users','windows','documents','downloads','appdata','local',
        'roaming','system32','desktop','qlikview','qliksense','apps',
    }
    seen = set()
    for t in tokens:
        v = t.strip()
        if len(v) < 2: continue
        if v.lower() in noise: continue
        if v.lower() in seen: continue
        if re.search(r'[/\\]', v): continue
        # Skip pure uppercase > 3 chars (binary constants)
        if v == v.upper() and len(v) > 3 and not re.search(r'\d', v): continue
        seen.add(v.lower())
        print(f"  {v}")

print("\nDONE")
