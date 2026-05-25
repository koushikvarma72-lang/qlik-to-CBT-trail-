import io
import os
import re
import sys


def find_qvf():
    if len(sys.argv) > 1:
        return sys.argv[1]

    uploads = os.path.join(os.path.dirname(__file__), "uploads")
    qvfs = []
    for root, _, files in os.walk(uploads):
        for name in files:
            if name.lower().endswith(".qvf"):
                qvfs.append(os.path.join(root, name))

    if not qvfs:
        raise FileNotFoundError("No .qvf file found in uploads/")

    return max(qvfs, key=os.path.getmtime)


def ascii_strings(raw, min_len=3):
    i = 0
    while i < len(raw):
        if 32 <= raw[i] <= 126 or raw[i] in (9, 10, 13):
            start = i
            while i < len(raw) and (32 <= raw[i] <= 126 or raw[i] in (9, 10, 13)):
                i += 1
            text = raw[start:i].decode("ascii", errors="replace").strip()
            if len(text) >= min_len:
                yield start, text
        else:
            i += 1


def split_multivalue_block(value):
    return [part.strip() for part in re.split(r"[\r\n\t]+", value) if part.strip()]


def extract_field_zone(raw):
    marker = raw.find(b"$Field")
    if marker == -1:
        return None, [], []

    strings = list(ascii_strings(raw, min_len=3))
    zone_strings = []
    split_values = []
    started = False

    for offset, value in strings:
        if not started and offset == marker and value == "$Field":
            started = True
        if not started:
            continue
        if offset > marker + 2500:
            break
        zone_strings.append((offset, value))
        split_values.extend(split_multivalue_block(value))

    return marker, zone_strings, split_values


def extract_path_records(raw):
    strings = list(ascii_strings(raw, min_len=20))
    path_records = []
    path_pattern = re.compile(r"^(.*?)([ -~])(C:\\.*\.(?:qvd|xlsx|xls|csv|txt|qvs))$", re.IGNORECASE)

    for offset, value in strings:
        match = path_pattern.match(value)
        if not match:
            continue
        name_part, separator, path = match.groups()
        path_records.append(
            {
                "offset": offset,
                "raw": value,
                "candidate_name": name_part,
                "separator": separator,
                "separator_ord": ord(separator),
                "path": path,
                "path_len": len(path),
                "separator_matches_path_len": ord(separator) == len(path),
            }
        )
    return path_records


def extract_expression_zone(raw):
    strings = list(ascii_strings(raw, min_len=3))
    items = []
    interesting = (
        "$#,##0.00",
        "h:mm:ss",
        "M/D/YYYY",
        "Concat(",
        "concat(",
        "Minstring(",
        "max(",
        "if(",
        '{"format":"gzjson"}',
    )
    for offset, value in strings:
        if any(token in value for token in interesting):
            items.append((offset, value))
    return items


def infer_relationship_hints(path_records, field_values):
    hints = []
    candidate_tables = [record["candidate_name"] for record in path_records]
    fields = [v for v in field_values if not v.startswith("$")]

    for table in candidate_tables:
        base = re.sub(r"(master|summary)$", "", table.lower()).replace("-", "")
        matched = []
        for field in fields:
            norm = re.sub(r"[^a-z0-9]", "", field.lower())
            if not norm:
                continue
            if base and (base in norm or norm in base):
                matched.append(field)
        if matched:
            hints.append((table, matched[:10]))

    return hints


def main():
    filepath = find_qvf()
    with open(filepath, "rb") as f:
        raw = f.read()

    out_path = "literal_qvf_report.txt"
    out = io.open(out_path, "w", encoding="utf-8", errors="replace")

    def w(line=""):
        print(line)
        out.write(line + "\n")

    w(f"FILE : {os.path.basename(filepath)}")
    w(f"SIZE : {len(raw):,} bytes")
    w(f"HEADER : {raw[:32].hex()}")
    w()

    w("=" * 78)
    w("SECTION 1: FIRST READABLE METADATA STRINGS")
    w("=" * 78)
    first_strings = [(offset, value) for offset, value in ascii_strings(raw, min_len=3) if offset < 5000]
    for offset, value in first_strings[:20]:
        w(f"0x{offset:07x}  [{value}]")
    w()

    w("=" * 78)
    w("SECTION 2: LITERAL $Field ZONE")
    w("=" * 78)
    marker, zone_strings, split_values = extract_field_zone(raw)
    if marker is None:
        w("$Field marker not found")
    else:
        w(f"$Field marker offset : 0x{marker:07x}")
        w("Raw readable blocks in the zone:")
        for offset, value in zone_strings:
            w(f"0x{offset:07x}  [{value}]")
        w()
        w("Split values from those literal blocks:")
        for idx, value in enumerate(split_values, 1):
            w(f"{idx:3d}. {value}")
    w()

    w("=" * 78)
    w("SECTION 3: LITERAL EXPRESSIONS / FORMATS / MARKERS")
    w("=" * 78)
    for offset, value in extract_expression_zone(raw):
        w(f"0x{offset:07x}  [{value}]")
    w()

    w("=" * 78)
    w("SECTION 4: LITERAL NAME + PATH BLOCKS")
    w("=" * 78)
    path_records = extract_path_records(raw)
    for record in path_records:
        w(f"0x{record['offset']:07x}  raw=[{record['raw']}]")
        w(f"           candidate_name=[{record['candidate_name']}]")
        w(f"           separator=[{record['separator']}] ord={record['separator_ord']}")
        w(f"           path=[{record['path']}]")
        w(f"           path_len={record['path_len']}  ord(separator)==path_len -> {record['separator_matches_path_len']}")
    w()

    w("=" * 78)
    w("SECTION 5: HOW TABLES / RELATIONSHIPS ARE BEING INFERRED")
    w("=" * 78)
    w("Table inference rule:")
    w("Take each literal block shaped like <name><1 printable byte><C:\\...file.ext>.")
    w("The final printable byte before C:\\ is treated as a separator/length byte, not part of the name,")
    w("because its ASCII code matches the exact path length in the same block.")
    w()
    w("Relationship inference rule:")
    w("There are no explicit plaintext joins or relationship declarations in the readable zones above.")
    w("Any relationship currently shown by the extractor is only a heuristic from shared field names,")
    w("not a direct fact proven by a readable relationship block in this QVF.")
    w()
    w("Name-to-field hints currently visible from literal text:")
    hints = infer_relationship_hints(path_records, split_values)
    if not hints:
        w("None")
    else:
        for table, fields in hints:
            w(f"{table}: {', '.join(fields)}")
    w()

    w(f"Report written to {out_path}")
    out.close()


if __name__ == "__main__":
    main()
