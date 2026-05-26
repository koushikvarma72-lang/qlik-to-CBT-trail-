# -*- coding: utf-8 -*-
import io, json, os, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', errors='replace')
sys.path.insert(0, '.')

from backend.extraction.qvf_runtime import extract_from_binary_qvf, generate_script_from_inferred_model

# Auto-find the most recently uploaded QVF
uploads = os.path.join(os.path.dirname(__file__), 'uploads')
qvfs = []
for root, _, files in os.walk(uploads):
    for f in files:
        if f.lower().endswith('.qvf'):
            qvfs.append(os.path.join(root, f))

if not qvfs:
    print("No QVF in uploads/"); sys.exit(1)

filepath = max(qvfs, key=os.path.getmtime)
print(f"Testing: {os.path.basename(filepath)}\n")

result = extract_from_binary_qvf(filepath)
model = result.get('associations') or {}
tables = model.get('tables', [])
assocs = model.get('associations', [])

print(f"TABLES FOUND: {len(tables)}")
for t in tables:
    fields = t.get('fields', [])
    src = t.get('sourceFile', '')
    print(f"  {t['name']:35s}  {len(fields):3d} fields  source={src}")

print(f"\nASSOCIATIONS FOUND: {len(assocs)}")
for a in assocs[:20]:
    print(f"  {a['fromTable']:30s} -> {a['toTable']:30s}  via [{a['fromFieldName']}]  ({a['relationship']})")

print("\nGENERATED SCRIPT:")
print("-" * 70)
script = result.get('script') or generate_script_from_inferred_model(model, os.path.basename(filepath))
print(script[:5000])
