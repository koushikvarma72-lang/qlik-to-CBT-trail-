# -*- coding: utf-8 -*-
"""
Verify exactly what is in the QVF binary — table names, paths, fields.
Writes output to verify_output.txt so nothing gets truncated.
"""
import io, os, re, sys, zipfile

filepath = None
for folder in ['uploads', 'samples']:
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower().endswith('.qvf'):
                filepath = os.path.join(root, f)
                break

if not filepath:
    print("No QVF found"); sys.exit(1)

out = io.open('verify_output.txt', 'w', encoding='utf-8')

def w(line=''):
    out.write(line + '\n')
    print(line)

w(f"FILE : {os.path.basename(filepath)}")
w(f"SIZE : {os.path.getsize(filepath):,} bytes")
w()

with open(filepath, 'rb') as f:
    raw = f.read()

text = raw.decode('utf-8', errors='replace')

# ── Section 1: JSON metadata at the start ────────────────────────────────────
w("=" * 70)
w("SECTION 1: JSON METADATA (first 2KB)")
w("=" * 70)
# Find all JSON-like blocks in the first 4KB
for m in re.finditer(r'\{[^{}]{20,500}\}', text[:4000]):
    w(f"  offset={m.start():6d}: {m.group()[:300]}")
w()

# ── Section 2: Table name + path pairs ───────────────────────────────────────
w("=" * 70)
w("SECTION 2: TABLE NAME + SOURCE FILE PAIRS")
w("(Every string that is immediately followed by a Windows path)")
w("=" * 70)

# Find all Windows paths in the binary
path_re = re.compile(r'([A-Za-z]:\\[^\x00\n\r\x01-\x1f]{10,250})')
for m in path_re.finditer(text):
    path = m.group(1).strip()
    # Only data files
    if not re.search(r'\.(qvd|xlsx?|xls|csv|txt|qvs)$', path, re.IGNORECASE):
        continue
    
    offset = m.start()
    # Look at the 60 bytes BEFORE the path to find the table name
    pre_start = max(0, offset - 80)
    pre_bytes = raw[pre_start:offset]
    
    # Decode the pre-bytes as ASCII, get the last readable run
    pre_text = ''
    for b in reversed(pre_bytes):
        if 32 <= b <= 126:
            pre_text = chr(b) + pre_text
        else:
            break
    
    # The last char before the path is the length byte — strip it
    table_name = pre_text[:-1].strip() if pre_text else '(unknown)'
    
    w(f"  TABLE : [{table_name}]")
    w(f"  PATH  : {path}")
    w(f"  BYTES_BEFORE: {pre_bytes[-10:].hex()}")
    w()

# ── Section 3: $Field metadata section ───────────────────────────────────────
w("=" * 70)
w("SECTION 3: ALL FIELD NAMES (from $Field metadata)")
w("=" * 70)

field_pos = text.find('$Field')
if field_pos >= 0:
    w(f"$Field found at offset {field_pos}")
    # Extract a large window
    chunk = text[field_pos:field_pos + 10000]
    
    # Find all readable tokens
    tokens = re.findall(r'[A-Za-z][A-Za-z0-9 .()\-/_]{1,80}', chunk)
    
    noise = {
        'field','table','rows','fields','fieldno','info','fileformat',
        'file32','filetype','usecompression','scramblea','scrambleb',
        'prohibitbinaryload','qvffileversion','buildno','format','gzjson',
        'usebinary','istemporary','contenthash','database','qvd','minstring',
        'language','jan','feb','mar','apr','may','jun','jul','aug','sep',
        'oct','nov','dec','mon','tue','wed','thu','fri','sat','sun',
        'users','windows','documents','downloads','appdata','local',
        'roaming','system32','desktop','qlikview','qliksense','apps',
        'cbo','program files','program',
    }
    
    seen = set()
    field_list = []
    for t in tokens:
        v = t.strip()
        if len(v) < 2: continue
        if v.lower() in noise: continue
        if v.lower() in seen: continue
        if re.search(r'[/\\]', v): continue
        # Skip pure uppercase > 3 chars (binary constants) unless has digit
        if v == v.upper() and len(v) > 3 and not re.search(r'\d', v): continue
        # Skip path fragments
        if re.search(r'^(Executive|Dashboard|Files|QlikView|Apps)$', v, re.IGNORECASE): continue
        seen.add(v.lower())
        field_list.append(v)
    
    w(f"Total unique field tokens: {len(field_list)}")
    w()
    for i, f in enumerate(field_list, 1):
        w(f"  {i:3d}. {f}")
else:
    w("  $Field marker NOT FOUND in this file")

# ── Section 4: Calculated expressions / variables ────────────────────────────
w()
w("=" * 70)
w("SECTION 4: EXPRESSIONS / VARIABLES")
w("=" * 70)
# Look for if(), sum(), count() etc.
expr_re = re.compile(r'(?:if|sum|count|avg|max|min|concat|num|date|year|month|floor|ceil|round)\s*\([^)]{5,200}\)', re.IGNORECASE)
seen_expr = set()
for m in expr_re.finditer(text):
    e = m.group().strip()
    if e.lower() not in seen_expr:
        seen_expr.add(e.lower())
        w(f"  {e[:200]}")

# SET/LET variables
w()
w("SET/LET variables:")
for m in re.finditer(r'(?:SET|LET)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*([^\n;]{0,120})', text, re.IGNORECASE):
    w(f"  {m.group(1)} = {m.group(2).strip()}")

out.close()
w()
w("Output written to verify_output.txt")
