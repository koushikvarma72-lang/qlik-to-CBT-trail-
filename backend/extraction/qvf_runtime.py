import csv
import io
import gzip
import json
import os
import re
import uuid
import zipfile
import zlib


# ─── Qlik Variable Resolution ────────────────────────────────────────────────

def extract_qlik_variables(script_text):
    """
    Parse all SET and LET statements from a Qlik script and return a dict of
    variable_name -> resolved_value.

    Handles:
      SET vVar = 'literal';
      LET vVar = 'literal';
      SET vVar = Today();          -- kept as-is (function call)
      SET vVar = $(vOtherVar);     -- resolved recursively up to 20 passes
    """
    if not script_text:
        return {}

    variables = {}

    # Match SET/LET assignments.  Value is everything up to the semicolon,
    # allowing multi-line values inside quotes.
    pattern = re.compile(
        r'(?im)^\s*(?:SET|LET)\s+([A-Za-z_][A-Za-z0-9_.]*)\s*=\s*(.*?)\s*;',
        re.DOTALL,
    )

    for match in pattern.finditer(script_text):
        var_name = match.group(1).strip()
        raw_value = match.group(2).strip()

        # Strip surrounding single quotes if present
        if raw_value.startswith("'") and raw_value.endswith("'") and len(raw_value) >= 2:
            raw_value = raw_value[1:-1]

        variables[var_name] = raw_value

    # Resolve cross-references: $(vOtherVar) inside values
    # Up to 20 passes to handle chains like vC = $(vB), vB = $(vA)
    ref_pattern = re.compile(r'\$\(([A-Za-z_][A-Za-z0-9_.]*)\)')
    for _ in range(20):
        changed = False
        for name, value in variables.items():
            def _replace(m):
                ref = m.group(1)
                return variables.get(ref, m.group(0))  # leave unresolved refs as-is
            new_value = ref_pattern.sub(_replace, value)
            if new_value != value:
                variables[name] = new_value
                changed = True
        if not changed:
            break

    return variables


def resolve_variables_in_script(script_text, variables=None):
    """
    Replace all $(varName) references in a Qlik script with their resolved
    values.  Unknown variables are left as-is so the AI can still see them.

    Returns (resolved_script, variables_dict, substitution_count).
    """
    if not script_text:
        return script_text, {}, 0

    if variables is None:
        variables = extract_qlik_variables(script_text)

    if not variables:
        return script_text, {}, 0

    ref_pattern = re.compile(r'\$\(([A-Za-z_][A-Za-z0-9_.]*)\)')
    substitution_count = 0

    def _replace(m):
        nonlocal substitution_count
        ref = m.group(1)
        if ref in variables:
            substitution_count += 1
            return variables[ref]
        return m.group(0)

    resolved = ref_pattern.sub(_replace, script_text)
    return resolved, variables, substitution_count


def prepare_script_for_migration(script_text):
    """
    Pre-process a Qlik script before sending it to the AI:
      1. Extract all SET/LET variables.
      2. Substitute $(varName) references with their resolved values.
      3. Prepend a variable summary comment so the AI understands the context.

    Returns the enriched script string.
    """
    if not script_text:
        return script_text

    variables = extract_qlik_variables(script_text)
    resolved_script, _, sub_count = resolve_variables_in_script(script_text, variables)

    if not variables:
        return script_text

    # Build a readable variable summary to prepend
    summary_lines = ['// ── Qlik Variable Definitions (auto-resolved) ──────────────────']
    for name, value in sorted(variables.items()):
        # Truncate very long values (e.g. long date expressions)
        display_value = value if len(value) <= 120 else value[:117] + '...'
        summary_lines.append(f'// {name} = {display_value}')
    summary_lines.append('// ────────────────────────────────────────────────────────────')
    summary_lines.append('')

    return '\n'.join(summary_lines) + resolved_script


def deep_scan_data(data):
    """Try to find Qlik script blocks in binary data."""
    text_blocks = []
    current_block = []
    for byte in data:
        if 32 <= byte <= 126 or byte in [9, 10, 13]:
            current_block.append(chr(byte))
        else:
            if len(current_block) > 40:
                text_blocks.append("".join(current_block))
            current_block = []
    if len(current_block) > 40:
        text_blocks.append("".join(current_block))

    keywords = ['LOAD', 'SELECT', 'SET', 'LET', 'STORE', 'DIRECTORY', 'CONNECT', 'FROM', 'RESIDENT']
    script_candidates = []
    for block in text_blocks:
        found_kws = [kw for kw in keywords if kw in block.upper()]
        if len(found_kws) >= 2:
            script_candidates.append(block)
    return script_candidates


def _find_ascii_strings(raw, min_len=3):
    strings = []
    i = 0
    while i < len(raw):
        if 32 <= raw[i] <= 126 or raw[i] in (9, 10, 13):
            start = i
            while i < len(raw) and (32 <= raw[i] <= 126 or raw[i] in (9, 10, 13)):
                i += 1
            text = raw[start:i].decode('ascii', errors='replace').strip()
            if len(text) >= min_len:
                strings.append((start, text))
        else:
            i += 1
    return strings


def _split_multivalue_ascii_block(value):
    return [part.strip() for part in re.split(r'[\r\n\t]+', value) if part.strip()]


def _collect_literal_evidence(raw):
    strings = _find_ascii_strings(raw, min_len=3)
    marker = raw.find(b'$Field')
    field_zone = {
        'markerOffset': marker if marker >= 0 else None,
        'rawBlocks': [],
        'splitValues': [],
    }
    if marker >= 0:
        started = False
        for offset, value in strings:
            if not started and offset == marker and value == '$Field':
                started = True
            if not started:
                continue
            if offset > marker + 2500:
                break
            field_zone['rawBlocks'].append({'offset': offset, 'text': value})
            field_zone['splitValues'].extend(_split_multivalue_ascii_block(value))

    path_blocks = []
    path_pattern = re.compile(
        r'^(.*?)([ -~])(C:\\.*\.(?:qvd|xlsx|xls|csv|txt|qvs))$',
        re.IGNORECASE,
    )
    for offset, value in strings:
        match = path_pattern.match(value)
        if not match:
            continue
        name_part, separator, path = match.groups()
        path_blocks.append({
            'offset': offset,
            'rawText': value,
            'candidateName': name_part,
            'separator': separator,
            'separatorOrd': ord(separator),
            'path': path,
            'pathLength': len(path),
            'separatorMatchesPathLength': ord(separator) == len(path),
        })

    expression_tokens = (
        '$#,##0.00',
        'h:mm:ss',
        'M/D/YYYY',
        'Concat(',
        'concat(',
        'Minstring(',
        'max(',
        'if(',
        '{"format":"gzjson"}',
    )
    expressions = [
        {'offset': offset, 'text': value}
        for offset, value in strings
        if any(token in value for token in expression_tokens)
    ]

    return {
        'headerHex': raw[:32].hex(),
        'firstReadableStrings': [
            {'offset': offset, 'text': value}
            for offset, value in strings
            if offset < 5000
        ][:20],
        'fieldZone': field_zone,
        'pathBlocks': path_blocks,
        'expressions': expressions,
    }


def _decode_bytes_to_text(payload):
    attempts = []
    for encoding in ('utf-8', 'utf-16-le', 'utf-16-be', 'latin-1'):
        try:
            text = payload.decode(encoding)
        except Exception as exc:
            attempts.append({'encoding': encoding, 'error': str(exc)})
            continue
        printable = sum(1 for ch in text if ch.isprintable() or ch in '\r\n\t')
        ratio = (printable / len(text)) if text else 0
        attempts.append({'encoding': encoding, 'printableRatio': ratio})
        if text and ratio >= 0.60:
            return text, encoding, attempts
    return None, None, attempts


def _extract_json_snippets(text):
    snippets = []
    decoder = json.JSONDecoder()
    idx = 0
    while idx < len(text):
        ch = text[idx]
        if ch not in '{[':
            idx += 1
            continue
        try:
            obj, end = decoder.raw_decode(text[idx:])
        except Exception:
            idx += 1
            continue
        snippets.append({
            'relativeOffset': idx,
            'type': type(obj).__name__,
            'preview': json.dumps(obj, ensure_ascii=False)[:400],
            'object': obj,
        })
        idx += max(end, 1)
    return snippets


def _attempt_payload_decompression(raw, candidate_offset, hard_limit=None):
    segment = raw[candidate_offset:hard_limit] if hard_limit is not None else raw[candidate_offset:]
    methods = [
        ('zlib', zlib.MAX_WBITS),
        ('gzip', zlib.MAX_WBITS | 16),
        ('raw-deflate', -zlib.MAX_WBITS),
    ]
    errors = []
    successes = []
    for name, wbits in methods:
        try:
            obj = zlib.decompressobj(wbits)
            payload = obj.decompress(segment)
            payload += obj.flush()
            consumed = len(segment) - len(obj.unused_data)
            if payload and consumed > 0:
                successes.append({
                    'method': name,
                    'payload': payload,
                    'consumed': consumed,
                    'unusedBytes': len(obj.unused_data),
                })
        except Exception as exc:
            errors.append({'method': name, 'error': str(exc)})

    try:
        payload = gzip.decompress(segment)
        if payload:
            successes.append({
                'method': 'gzip-module',
                'payload': payload,
                'consumed': len(segment),
                'unusedBytes': 0,
            })
    except Exception as exc:
        errors.append({'method': 'gzip-module', 'error': str(exc)})

    if not successes:
        return None, errors

    best = max(successes, key=lambda item: (len(item['payload']), item['consumed']))
    return best, errors


def _scan_gzjson_sections(raw):
    marker = b'{"format":"gzjson"}'
    marker_offsets = []
    start = 0
    while True:
        pos = raw.find(marker, start)
        if pos == -1:
            break
        marker_offsets.append(pos)
        start = pos + 1

    decoded_sections = []
    undecoded_sections = []
    for index, marker_offset in enumerate(marker_offsets):
        next_marker = marker_offsets[index + 1] if index + 1 < len(marker_offsets) else len(raw)
        payload_search_start = marker_offset + len(marker)
        payload_window = raw[payload_search_start:next_marker]

        candidate_positions = []
        for signature in (b'\x78\x9c', b'\x78\xda', b'\x78\x01', b'\x78\x5e', b'\x1f\x8b'):
            search_pos = payload_window.find(signature)
            if search_pos != -1:
                candidate_positions.append(payload_search_start + search_pos)

        if not candidate_positions:
            candidate_positions = [payload_search_start]
        candidate_positions = list(dict.fromkeys(candidate_positions))

        best_result = None
        attempt_log = []
        for candidate_offset in candidate_positions[:8]:
            result, errors = _attempt_payload_decompression(raw, candidate_offset, next_marker)
            attempt_log.append({
                'candidateOffset': candidate_offset,
                'errors': errors,
                'success': bool(result),
                'method': result['method'] if result else None,
                'payloadBytes': len(result['payload']) if result else 0,
                'consumedBytes': result['consumed'] if result else 0,
            })
            if result and (best_result is None or len(result['payload']) > len(best_result['payload'])):
                best_result = {
                    'candidateOffset': candidate_offset,
                    **result,
                }

        if not best_result:
            undecoded_sections.append({
                'index': index,
                'markerOffset': marker_offset,
                'payloadSearchStart': payload_search_start,
                'nextMarkerOffset': next_marker,
                'status': 'undecoded',
                'errors': attempt_log,
            })
            continue

        decoded_text, text_encoding, text_attempts = _decode_bytes_to_text(best_result['payload'])
        json_snippets = _extract_json_snippets(decoded_text) if decoded_text else []
        decoded_end = best_result['candidateOffset'] + best_result['consumed']
        section = {
            'index': index,
            'markerOffset': marker_offset,
            'payloadSearchStart': payload_search_start,
            'payloadStart': best_result['candidateOffset'],
            'payloadEnd': decoded_end,
            'nextMarkerOffset': next_marker,
            'compressionMethod': best_result['method'],
            'status': 'decoded',
            'decodedBytes': len(best_result['payload']),
            'consumedBytes': best_result['consumed'],
            'unusedBytes': best_result['unusedBytes'],
            'textEncoding': text_encoding,
            'textPreview': decoded_text[:2000] if decoded_text else '',
            'jsonSnippetCount': len(json_snippets),
            'jsonSnippets': [
                {
                    'relativeOffset': snippet['relativeOffset'],
                    'type': snippet['type'],
                    'preview': snippet['preview'],
                }
                for snippet in json_snippets[:20]
            ],
            'textDecodeAttempts': text_attempts,
            'attemptLog': attempt_log,
            '_decodedText': decoded_text,
            '_decodedPayload': best_result['payload'],
            '_decodedJsonObjects': [snippet['object'] for snippet in json_snippets[:20]],
        }
        decoded_sections.append(section)

    return {
        'markerOffsets': marker_offsets,
        'decodedSections': decoded_sections,
        'undecodedSections': undecoded_sections,
    }


def _section_might_contain_script(text, exclude_json_documents=True):
    if not text:
        return False

    # Exclude whole valid JSON objects/arrays (e.g. metadata, config, dashboard
    # layout). Callers that have already extracted a leaf string from JSON can
    # disable this so nested qScript/script values are still considered.
    stripped = text.strip()
    if exclude_json_documents and (
        (stripped.startswith('{') and stripped.endswith('}'))
        or (stripped.startswith('[') and stripped.endswith(']'))
    ):
        try:
            json.loads(stripped)
            return False  # It is a valid JSON data structure, not a raw Qlik script
        except Exception:
            pass

    upper = text.upper()
    return (
        'LOAD' in upper
        or 'SELECT' in upper
        or 'SET ' in upper
        or 'LET ' in upper
        or 'FROM [' in upper
        or 'RESIDENT' in upper
    )


def _collect_decoded_script_candidates(decoded_sections):
    candidates = []
    seen = set()
    for section in decoded_sections:
        decoded_text = section.get('_decodedText') or ''
        if not decoded_text:
            continue
        if _section_might_contain_script(decoded_text):
            normalized = decoded_text.strip()
            if normalized and normalized not in seen:
                seen.add(normalized)
                candidates.append({
                    'sectionIndex': section['index'],
                    'source': 'decoded_gzjson_text',
                    'text': normalized,
                })
        for obj in section.get('_decodedJsonObjects', []):
            if isinstance(obj, dict):
                for key in ('script', 'qScript', 'definition', 'text'):
                    value = obj.get(key)
                    if isinstance(value, str) and _section_might_contain_script(value, exclude_json_documents=False):
                        normalized = value.strip()
                        if normalized and normalized not in seen:
                            seen.add(normalized)
                            candidates.append({
                                'sectionIndex': section['index'],
                                'source': f'decoded_gzjson_json.{key}',
                                'text': normalized,
                            })
    return candidates


def _write_binary_artifacts(filepath, extract_dir, literal_evidence, scan_result, script_candidates, inferred_model):
    artifact_dir = os.path.join(extract_dir, 'binary_forensics')
    os.makedirs(artifact_dir, exist_ok=True)

    manifest = {
        'file': os.path.basename(filepath),
        'literalEvidence': literal_evidence,
        'decodedSections': [],
        'undecodedSections': scan_result['undecodedSections'],
        'scriptCandidates': [
            {
                'sectionIndex': candidate['sectionIndex'],
                'source': candidate['source'],
                'preview': candidate['text'][:800],
            }
            for candidate in script_candidates
        ],
        'heuristicModelAvailable': bool(inferred_model),
    }

    report_lines = [
        f"FILE : {os.path.basename(filepath)}",
        f"ARTIFACT DIR : {artifact_dir}",
        "",
        "=== Literal from bytes ===",
        f"Header : {literal_evidence['headerHex']}",
        f"Field marker offset : {literal_evidence['fieldZone']['markerOffset']}",
        f"Path blocks : {len(literal_evidence['pathBlocks'])}",
        f"Expression blocks : {len(literal_evidence['expressions'])}",
        "",
        "=== Decoded from compressed payload ===",
        f"gzjson markers : {len(scan_result['markerOffsets'])}",
        f"decoded sections : {len(scan_result['decodedSections'])}",
        f"undecoded sections : {len(scan_result['undecodedSections'])}",
        "",
    ]

    for section in scan_result['decodedSections']:
        base_name = f"decoded_section_{section['index']:03d}"
        payload_filename = f"{base_name}.bin"
        payload_path = os.path.join(artifact_dir, payload_filename)
        with open(payload_path, 'wb') as f:
            f.write(section['_decodedPayload'])

        text_filename = None
        if section.get('_decodedText'):
            text_filename = f"{base_name}.txt"
            text_path = os.path.join(artifact_dir, text_filename)
            with open(text_path, 'w', encoding='utf-8', errors='replace') as f:
                f.write(section['_decodedText'])

        manifest['decodedSections'].append({
            'index': section['index'],
            'markerOffset': section['markerOffset'],
            'payloadStart': section['payloadStart'],
            'payloadEnd': section['payloadEnd'],
            'compressionMethod': section['compressionMethod'],
            'status': section['status'],
            'decodedBytes': section['decodedBytes'],
            'consumedBytes': section['consumedBytes'],
            'unusedBytes': section['unusedBytes'],
            'textEncoding': section['textEncoding'],
            'textPreview': section['textPreview'],
            'jsonSnippetCount': section['jsonSnippetCount'],
            'jsonSnippets': section['jsonSnippets'],
            'payloadFile': payload_filename,
            'textFile': text_filename,
            'attemptLog': section['attemptLog'],
        })
        report_lines.extend([
            f"[decoded section {section['index']}] marker=0x{section['markerOffset']:x} method={section['compressionMethod']}",
            f"payloadStart=0x{section['payloadStart']:x} payloadEnd=0x{section['payloadEnd']:x} decodedBytes={section['decodedBytes']}",
            f"textEncoding={section['textEncoding']} jsonSnippetCount={section['jsonSnippetCount']}",
            f"preview={section['textPreview'][:300]}",
            "",
        ])

    report_lines.append("=== Heuristic / inferred ===")
    if inferred_model:
        report_lines.append(f"heuristic tables={len(inferred_model.get('tables', []))} associations={len(inferred_model.get('associations', []))}")
    else:
        report_lines.append("no heuristic model built")
    report_lines.append("")

    manifest_path = os.path.join(artifact_dir, 'binary_manifest.json')
    with open(manifest_path, 'w', encoding='utf-8') as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)

    report_path = os.path.join(artifact_dir, 'binary_report.txt')
    with open(report_path, 'w', encoding='utf-8', errors='replace') as f:
        f.write('\n'.join(report_lines))

    summary_path = os.path.join(artifact_dir, 'undecoded_sections.json')
    with open(summary_path, 'w', encoding='utf-8') as f:
        json.dump(scan_result['undecodedSections'], f, indent=2, ensure_ascii=False)

    return {
        'artifactDir': artifact_dir,
        'manifestPath': manifest_path,
        'reportPath': report_path,
        'undecodedSummaryPath': summary_path,
    }


def _print_binary_terminal_summary(filepath, binary_report, literal_evidence, decoded_sections, undecoded_sections, inferred_model, script_text):
    print("")
    print("=" * 78)
    print(f"BINARY QVF FORENSICS | {os.path.basename(filepath)}")
    print("=" * 78)
    print("Confirmed in file (literal bytes)")
    print(f"  Header: {literal_evidence.get('headerHex', '')}")
    print(f"  Field marker offset: {literal_evidence.get('fieldZone', {}).get('markerOffset')}")
    print(f"  Path blocks found: {len(literal_evidence.get('pathBlocks', []))}")
    for block in literal_evidence.get('pathBlocks', [])[:8]:
        print(
            f"    0x{block['offset']:x} | {block['candidateName']} -> {block['path']}"
        )
    print("")
    print("Decoded from compressed payload")
    print(f"  gzjson markers: {binary_report.get('gzjsonMarkerCount', 0)}")
    print(f"  Decoded sections: {len(decoded_sections)}")
    print(f"  Undecoded sections: {len(undecoded_sections)}")
    for section in decoded_sections[:8]:
        print(
            f"    section {section['index']} | marker=0x{section['markerOffset']:x} "
            f"| method={section['compressionMethod']} | bytes={section['decodedBytes']}"
        )
    if undecoded_sections:
        print("  Undecoded markers")
        for section in undecoded_sections[:8]:
            print(
                f"    section {section['index']} | marker=0x{section['markerOffset']:x} "
                f"| status={section['status']}"
            )
    print("")
    print("Extracted / inferred result")
    print(f"  Script source: {binary_report.get('scriptSource')}")
    print(f"  Script found: {bool(script_text)}")
    if inferred_model:
        print(f"  Tables extracted: {len(inferred_model.get('tables', []))}")
        print(f"  Associations extracted: {len(inferred_model.get('associations', []))}")
        for table in inferred_model.get('tables', [])[:8]:
            print(
                f"    {table['name']} | fields={len(table.get('fields', []))} "
                f"| source={table.get('sourceFile', '')}"
            )
    else:
        print("  No inferred table model built")
    print("=" * 78)
    print("")


def looks_like_qlik_script_block(block):
    """Return True only for blocks that resemble real Qlik script."""
    if not block:
        return False

    upper = block.upper()
    keywords = ['LOAD', 'SELECT', 'SET ', 'LET ', 'STORE ', 'CONNECT', 'RESIDENT']
    if not any(keyword in upper for keyword in keywords):
        return False

    if not re.search(r'(?m)^\s*(\[.*\]|[A-Za-z0-9_]+:)', block):
        return False

    if ';' not in block:
        return False

    return True


def singularize_name(name):
    """Convert a plural-looking table name into a singular form."""
    lowered = name.lower()
    if lowered.endswith('ies') and len(name) > 3:
        return name[:-3] + 'y'
    if lowered.endswith('ses') and len(name) > 3:
        return name[:-2]
    if lowered.endswith('s') and len(name) > 3:
        return name[:-1]
    return name


def infer_relationship_cardinality(source_table, target_table, field_name):
    """
    Infer if the relationship between source_table and target_table on field_name
    is '1:*', '*:1', '1:1', or 'shared_field'.
    """
    field_lower = field_name.lower().strip('%_')
    for suffix in ['id', 'key', 'code']:
        if field_lower.endswith(suffix):
            field_lower = field_lower[:-len(suffix)].strip('%_')
            break

    source_lower = source_table.lower()
    target_lower = target_table.lower()

    source_sing = singularize_name(source_lower)
    target_sing = singularize_name(target_lower)
    field_sing = singularize_name(field_lower)

    # Heuristic 1: If one table name matches the key entity name, it's likely the "one" side.
    source_is_entity = (source_sing == field_sing or source_lower == field_lower)
    target_is_entity = (target_sing == field_sing or target_lower == field_lower)

    if source_is_entity and not target_is_entity:
        return '1:*'
    elif target_is_entity and not source_is_entity:
        return '*:1'
    elif source_is_entity and target_is_entity:
        return '1:1'

    # Heuristic 2: Look at table names for common fact/dimension patterns.
    # Dim tables often contain 'dim', 'master', 'ref', 'parameter', 'metadata', 'config'
    # Fact tables often contain 'fact', 'transaction', 'sales', 'order', 'history', 'ledger'
    source_is_dim = any(kw in source_lower for kw in ['dim', 'master', 'ref', 'parameter', 'metadata'])
    target_is_dim = any(kw in target_lower for kw in ['dim', 'master', 'ref', 'parameter', 'metadata'])
    source_is_fact = any(kw in source_lower for kw in ['fact', 'transaction', 'sales', 'order', 'ledger'])
    target_is_fact = any(kw in target_lower for kw in ['fact', 'transaction', 'sales', 'order', 'ledger'])

    if (source_is_dim and target_is_fact) or (source_is_dim and not target_is_dim):
        return '1:*'
    elif (target_is_dim and source_is_fact) or (target_is_dim and not source_is_dim):
        return '*:1'

    return 'shared_field'


def _extract_binary_table_labels(text_data):
    labels = []
    for match in re.finditer(r'([A-Za-z_][A-Za-z0-9_ ]{0,80})\+lib://[^\s\x00]+', text_data or ''):
        label = re.sub(r'\s+', ' ', match.group(1)).strip()
        if not label:
            continue
        label = label.split('/')[-1].strip()
        if label and label not in labels:
            labels.append(label)
    return labels


def _is_suspicious_binary_token(value):
    if not value:
        return False
    if re.fullmatch(r'(..)\1{4,}', value) or re.fullmatch(r'(.)\1{5,}', value):
        return True
    letters_only = re.sub(r'[^A-Za-z]', '', value)
    if len(letters_only) >= 12 and len(set(letters_only.lower())) <= 2:
        return True
    return False


def _extract_binary_field_names(text_data):
    """
    Extract real field names from the $Field metadata section.

    The $Field section stores field names as readable strings, but is
    immediately followed by compressed app-object metadata that produces
    garbage tokens. We stop extraction at the first sign of garbage:
    - Tokens shorter than 3 chars
    - Base64-looking hashes (long alphanumeric with mixed case, no spaces)
    - Tokens matching 'field_N' or 'table_N' patterns (internal metadata)
    - Tokens that are clearly binary fragments
    """
    marker = '$Field'
    start = (text_data or '').find(marker)
    if start == -1:
        return []

    # Use a window large enough to capture all fields
    chunk = text_data[start:start + 8000]
    tokens = re.findall(r'[A-Za-z][A-Za-z0-9 .()\-/_]{1,80}', chunk)

    blacklist = {
        '$field', '$table', '$rows', '$fields', '$fieldno', '$info',
        'field', 'table', 'rows', 'fields', 'fieldno', 'info',
        'fileformat', 'file32', 'filetype', 'usecompression', 'scramblea',
        'scrambleb', 'prohibitbinaryload', 'qvffileversion', 'buildno',
        'format', 'gzjson', 'usebinary', 'istemporary', 'contenthash',
        'database', 'qvd', 'minstring', 'language', 'type', 'title',
        'parentid', 'false', 'true', 'null',
    }

    def _is_garbage(token):
        """Return True if this token is binary garbage, not a real field name."""
        # Internal metadata patterns
        if re.match(r'^(field|table)_\d+$', token, re.IGNORECASE):
            return True
        # Base64 / hash fragments (long, no spaces, mixed case, no common words)
        if len(token) > 20 and re.match(r'^[A-Za-z0-9+/=_\-]+$', token):
            return True
        # Very short tokens (2 chars) that aren't meaningful
        if len(token) <= 2:
            return True
        # Tokens that look like code fragments
        if re.search(r'[(){}[\]<>]', token) and len(token) < 6:
            return True
        # App object entry names
        if re.search(r'(AppObject|Entry|cApp|vapp|qvapp)', token, re.IGNORECASE):
            return True
        return False

    fields = []
    seen = set()
    garbage_streak = 0  # Stop after 3 consecutive garbage tokens

    for token in tokens:
        value = re.sub(r'\s+', ' ', token).strip(' .,:;+-')
        if not value:
            continue

        lower_value = value.lower()

        if lower_value in blacklist:
            continue
        if lower_value in seen:
            continue
        if re.search(r'[/\\]', value):
            continue
        # Skip pure uppercase > 3 chars (binary constants) unless has digit
        if value == value.upper() and len(value) > 3 and not re.search(r'\d', value):
            continue
        if _is_suspicious_binary_token(value):
            continue

        if _is_garbage(value):
            garbage_streak += 1
            if garbage_streak >= 3:
                break  # We've hit the metadata section — stop
            continue

        garbage_streak = 0  # Reset on a good token
        seen.add(lower_value)
        fields.append(value)

    return fields


def _extract_tables_from_binary_paths(text_data):
    """
    Extract table names and source file paths from binary QVF data.

    From raw binary inspection, the format is:
      <TableName><1_byte_path_length><C:\\path\\to\\file.ext>

    The single character between the table name and 'C:\\' is a binary
    length byte that happens to fall in the printable ASCII range
    (e.g. 'Z'=90, 'U'=85, 'V'=86, 'Y'=89, 'R'=82, ']'=93, 'P'=80).
    It is NOT part of the table name — we strip it.

    Confirmed table entries from raw dump:
      AccountGroupMasterZ  -> AccountGroupMaster.qvd
      AccountMasterU       -> AccountMaster.qvd
      BudgetV              -> RegionalSales.xlsx
      InventoryBalancesY   -> InventoryBalances.qvd
      ItemMasterR          -> ItemMaster.qvd
      ProductSubGroupMaster] -> ProductSubGroupMaster.qvd
      ProductTypeMasterY   -> ProductTypeMaster.qvd
      ProductGroupMasterZ  -> ProductGroupMaster.qvd
      ItemBranchMasterX    -> ItemBranchMaster.qvd
      SalesRepMasterV      -> SalesRepMaster.qvd
      FactTableP           -> Expenses.xls
      ExpensesP            -> Expenses.xls
      HistoryFlagP         -> Expenses.xls
      AccountsW            -> ExpenseAccounts.xls
      CustomerAddressMaster] -> CustomerAddressMaster.qvd
      CustomerMapS         -> CustomerMap.qvd
      CustomerMasterV      -> CustomerMaster.qvd
      ChannelMasterU       -> ChannelMaster.qvd
      CalendarP            -> Calendar.qvd
      ARSummaryQ           -> ARSummary.qvd
      ARSummary-1S         -> ARSummary-1.qvd
    """
    tables = []
    seen_paths = set()

    # Pattern: (table_name + 1 length byte)(windows absolute path to data file)
    # The length byte is a single printable ASCII char immediately before C:\
    path_pattern = re.compile(
        r'([A-Za-z_][A-Za-z0-9_ \-]{1,60})'   # table name + trailing length byte
        r'([A-Za-z]:\\[^\x00\n\r\x01-\x1f]{10,250})'  # windows path
    )

    for match in path_pattern.finditer(text_data or ''):
        raw_name_with_byte = match.group(1)
        raw_path = match.group(2).strip()

        # Only data files
        if not re.search(r'\.(qvd|xlsx?|xls|csv|txt|qvs)$', raw_path, re.IGNORECASE):
            continue

        # Deduplicate by path
        path_key = raw_path.lower()
        if path_key in seen_paths:
            continue
        seen_paths.add(path_key)

        # Strip the trailing length byte — it is the last character
        # of the matched name group (the single byte before C:\)
        table_name = raw_name_with_byte[:-1].strip()

        if not table_name or len(table_name) < 2:
            continue

        # Skip OS/path fragments that got matched
        skip = {
            'users', 'windows', 'program', 'documents', 'downloads',
            'appdata', 'local', 'roaming', 'system32', 'desktop',
            'qlikview', 'qliksense', 'apps', 'executive',
        }
        if table_name.lower() in skip:
            continue

        ext = os.path.splitext(raw_path)[1].lower()
        source_type_map = {
            '.qvd': 'QVD', '.xlsx': 'Excel', '.xls': 'Excel',
            '.csv': 'CSV', '.txt': 'Text', '.qvs': 'Script',
        }
        source_type = source_type_map.get(ext, 'File')
        source_file = os.path.basename(raw_path)

        tables.append({
            'name': table_name,
            'source_file': source_file,
            'source_path': raw_path,
            'source_type': source_type,
        })

    return tables


def infer_binary_model(text_data):
    """
    Infer a complete table/relationship model from embedded binary strings.

    Strategy (in priority order):
    1. Extract table names + source file paths from binary path patterns
    2. Extract field names from the $Field metadata section
    3. Assign fields to tables using name-matching heuristics
    4. Infer associations from shared field names across tables
    """
    if not text_data:
        return None

    # ── Step 1: Extract tables from binary path patterns ─────────────────────
    path_tables = _extract_tables_from_binary_paths(text_data)

    # ── Step 2: Extract all field names from $Field metadata ─────────────────
    all_fields = _extract_binary_field_names(text_data)

    # Filter out noise tokens (paths, OS names, etc.)
    noise_patterns = re.compile(
        r'^(users|windows|program|documents|downloads|appdata|'
        r'local|roaming|system32|desktop|qlikview|qliksense|'
        r'jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec|'
        r'mon|tue|wed|thu|fri|sat|sun|'
        r'sum|concat|distinct|database|qvd|minstring|max|if|'
        r'mm|ss|tt|fff|dA)$',
        re.IGNORECASE
    )
    clean_fields = [
        f for f in all_fields
        if not noise_patterns.match(f)
        and not re.search(r'[/\\]', f)
        and not f.startswith('lib://')
        and len(f) >= 2
    ]

    # ── Step 3: Build tables ──────────────────────────────────────────────────
    tables = []

    if path_tables:
        # We have real table names from the binary — assign fields by name matching
        for pt in path_tables:
            tname = pt['name']
            tname_lower = tname.lower().replace(' ', '').replace('_', '')

            # Find fields that likely belong to this table:
            # - field name contains the table name
            # - or field name is a known key pattern for this table
            table_fields = []
            seen_fields = set()

            for field in clean_fields:
                field_lower = field.lower().replace(' ', '').replace('_', '')
                field_key = field.lower()

                if field_key in seen_fields:
                    continue

                # Assign field to this table if:
                # 1. Field name starts with or contains the table name (minus common suffixes)
                base_name = re.sub(r'(master|summary|detail|data|table|fact|dim)$', '', tname_lower)
                if base_name and (field_lower.startswith(base_name) or base_name in field_lower):
                    seen_fields.add(field_key)
                    is_key = bool(re.search(r'(id|key|code|num|no)$', field_lower))
                    table_fields.append({
                        'name': field,
                        'type': _infer_field_type(field),
                        'isKey': is_key,
                    })

            # Always add a primary key field if none found
            if not any(f['isKey'] for f in table_fields):
                # Look for a field that matches TableNameID or TableNameKey
                for field in clean_fields:
                    field_lower = field.lower().replace(' ', '').replace('_', '')
                    base_name = re.sub(r'(master|summary|detail|data|table|fact|dim)$', '', tname_lower)
                    if base_name and field_lower == f'{base_name}id':
                        if field.lower() not in seen_fields:
                            seen_fields.add(field.lower())
                            table_fields.insert(0, {
                                'name': field,
                                'type': 'varchar',
                                'isKey': True,
                            })
                        break

            tables.append({
                'id': f"binary_{re.sub(r'[^a-z0-9]', '_', tname.lower())}",
                'name': tname,
                'fields': table_fields,
                'rows': 0,
                'description': f"Loaded from {pt['source_file']} ({pt['source_type']})",
                'sourceFile': pt['source_file'],
                'sourcePath': pt['source_path'],
                'sourceType': pt['source_type'],
            })

    elif all_fields:
        # No path tables found — fall back to a single table with all fields
        fields = []
        seen = set()
        for field_name in clean_fields:
            normalized = field_name.lower()
            if normalized in seen:
                continue
            seen.add(normalized)
            is_key = bool(re.search(r'(id|key|code|num|no)$', normalized))
            fields.append({
                'name': field_name,
                'type': _infer_field_type(field_name),
                'isKey': is_key,
            })
        if fields:
            tables.append({
                'id': 'binary_extracted',
                'name': 'ExtractedData',
                'fields': fields,
                'rows': 0,
            })

    if not tables:
        return None

    # ── Step 4: Infer associations from shared field names ───────────────────
    # Build a map of field_name_lower -> [table_names]
    field_to_tables = {}
    for table in tables:
        for field in table.get('fields', []):
            key = field['name'].lower()
            field_to_tables.setdefault(key, []).append(table['name'])

    associations = []
    seen_pairs = set()
    table_map = {t['name']: t for t in tables}

    for field_name, tnames in field_to_tables.items():
        unique = list(dict.fromkeys(tnames))  # preserve order, deduplicate
        if len(unique) < 2:
            continue
        for i, src in enumerate(unique[:-1]):
            for tgt in unique[i + 1:]:
                pair = tuple(sorted((src.lower(), tgt.lower(), field_name)))
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                # Find the actual field name (with original casing)
                src_field = next(
                    (f['name'] for f in table_map[src]['fields'] if f['name'].lower() == field_name),
                    field_name
                )
                tgt_field = next(
                    (f['name'] for f in table_map[tgt]['fields'] if f['name'].lower() == field_name),
                    field_name
                )
                cardinality = infer_relationship_cardinality(src, tgt, src_field)

                associations.append({
                    'id': f"binary_{src.lower()}_{tgt.lower()}_{field_name}",
                    'fromTable': src,
                    'fromTableId': table_map[src]['id'],
                    'fromFieldName': src_field,
                    'toTable': tgt,
                    'toTableId': table_map[tgt]['id'],
                    'toFieldName': tgt_field,
                    'relationship': cardinality,
                })

    return {'tables': tables, 'associations': associations}


def _infer_field_type(field_name):
    """Infer a SQL type from a field name."""
    name = field_name.lower().replace(' ', '_').replace('-', '_')
    if re.search(r'(date|_dt|_at|timestamp|month|year|quarter|day)$', name):
        return 'date'
    if re.search(r'(amount|qty|quantity|count|total|price|cost|revenue|sales|'
                 r'gross|balance|budget|actual|margin|_id|id$|key$|num$|no$|'
                 r'number$|code$)$', name):
        return 'number'
    if re.search(r'(is_|has_|flag$|_flag$)$', name):
        return 'boolean'
    return 'varchar'


def generate_script_from_inferred_model(model, filename):
    """
    Create a readable Qlik-style LOAD script from the inferred binary model.
    Uses the actual source file paths extracted from the binary where available.
    """
    lines = [
        f"// ── RECONSTRUCTED FROM BINARY QVF: {filename} ──────────────────────",
        "// This script was inferred from the binary metadata.",
        "// Source file paths are the original paths embedded in the QVF.",
        "",
    ]

    for table in model.get('tables', []):
        tname = table['name']
        fields = table.get('fields', [])
        source_path = table.get('sourcePath', '')
        source_file = table.get('sourceFile', '')
        source_type = table.get('sourceType', 'QVD')

        # Build field list
        if fields:
            field_lines = []
            for f in fields:
                fname = f['name']
                if ' ' in fname or any(c in fname for c in '.-()'):
                    field_lines.append(f'    [{fname}]')
                else:
                    field_lines.append(f'    {fname}')
            field_list = ',\n'.join(field_lines)
        else:
            field_list = '    *'

        lines.append(f"[{tname}]:")
        lines.append(f"LOAD")
        lines.append(field_list)

        if source_path:
            # Use the actual path from the binary
            lines.append(f"FROM [{source_path}]")
            if source_type == 'QVD':
                lines.append("(qvd);")
            elif source_type == 'Excel':
                lines.append("(ooxml, embedded labels, table is Sheet1);")
            elif source_type == 'CSV':
                lines.append("(txt, utf8, embedded labels, delimiter is ',');")
            else:
                lines.append(";")
        else:
            lines.append(";")

        lines.append("")

    return "\n".join(lines).strip()


def generate_script_from_filename(filename):
    """Generate a sample Qlik script from filename for binary QVF files."""
    words = re.split(r'[\s_]+', filename)
    table_names = [w.capitalize() for w in words if w and len(w) > 2]

    if not table_names:
        table_names = ['Data']

    script_lines = [f"// --- GENERATED FROM BINARY: {filename} ---"]
    for table in table_names[:5]:
        script_lines.append(f"[{table}]:")
        script_lines.append(f"LOAD * FROM [lib://Data/{table}.qvd];")
        script_lines.append("")

    script_lines.append("SET ThousandSep=',';")
    script_lines.append("SET DecimalSep='.';")
    script_lines.append("SET MoneyFormat='$#,##0.00';")
    script_lines.append("SET DateFormat='MM/DD/YYYY';")

    return "\n".join(script_lines)


def generate_associations_from_filename(filename):
    """Generate associations from filename for binary QVF files."""
    words = re.split(r'[\s_]+', filename)
    table_names = [w.capitalize() for w in words if w and len(w) > 2]

    if not table_names:
        table_names = ['Data']

    tables = []
    common_fields = ['ID', 'Name', 'Date', 'Value', 'Quantity', 'Amount']

    for i, table in enumerate(table_names[:5]):
        fields = [{'name': f'{table}ID', 'type': 'varchar', 'isKey': i == 0}]
        for field in common_fields[:4]:
            fields.append({'name': field, 'type': 'varchar', 'isKey': False})

        tables.append({
            'id': f'gen_{table}',
            'name': table,
            'fields': fields,
            'rows': 1000 + (i * 100)
        })

    associations = []
    for i in range(len(tables) - 1):
        associations.append({
            'id': f'assoc_{i}',
            'fromTable': tables[i]['name'],
            'fromFieldName': f'{tables[i]["name"]}ID',
            'toTable': tables[i + 1]['name'],
            'toFieldName': f'{tables[i + 1]["name"]}ID',
            'relationship': '1:1'
        })

    return {'tables': tables, 'associations': associations}


def parse_inline_table_samples(script_text, max_rows=5000):
    """Extract previewable row data from Qlik INLINE LOAD blocks."""
    if not script_text:
        return {}

    inline_tables = {}
    pattern = re.compile(
        r'(?:\[([^\]]+)\]|([A-Za-z0-9_\$]+)):\s*LOAD[\s\S]*?\bINLINE\s*\[([\s\S]*?)\]\s*;',
        re.IGNORECASE,
    )

    for match in pattern.finditer(script_text):
        table_name = (match.group(1) or match.group(2) or '').strip()
        inline_body = match.group(3) or ''
        lines = [
            line.strip()
            for line in inline_body.replace('\r\n', '\n').replace('\r', '\n').split('\n')
            if line.strip() and not line.strip().startswith('//')
        ]
        if not table_name or len(lines) < 2:
            continue

        try:
            reader = csv.reader(io.StringIO('\n'.join(lines)), skipinitialspace=True)
            parsed_rows = [row for row in reader if row]
        except Exception:
            continue

        if len(parsed_rows) < 2:
            continue

        headers = [cell.strip().strip('"').strip("'") for cell in parsed_rows[0]]
        if not any(headers):
            continue

        rows = []
        for raw_row in parsed_rows[1:max_rows + 1]:
            row_obj = {}
            for index, header in enumerate(headers):
                if not header:
                    continue
                row_obj[header] = raw_row[index].strip() if index < len(raw_row) else ''
            if row_obj:
                rows.append(row_obj)

        if rows:
            inline_tables[table_name] = {
                'columns': headers,
                'rows': rows,
                'rowCount': len(parsed_rows) - 1,
                'truncated': len(parsed_rows) - 1 > len(rows),
            }

    return inline_tables


def attach_inline_samples_to_tables(tables, script_text):
    inline_samples = parse_inline_table_samples(script_text)
    if not inline_samples:
        return tables

    table_index = {str(table.get('name', '')).lower(): table for table in tables}
    for table_name, sample in inline_samples.items():
        key = table_name.lower()
        table = table_index.get(key)
        if not table:
            table = {
                'id': f"inline_{table_name}",
                'name': table_name,
                'fields': [],
                'rows': sample['rowCount'],
            }
            tables.append(table)
            table_index[key] = table

        table['dataRows'] = sample['rows']
        table['dataColumns'] = sample['columns']
        table['dataRowCount'] = sample['rowCount']
        table['dataTruncated'] = sample['truncated']
        if not table.get('rows'):
            table['rows'] = sample['rowCount']
        if not table.get('fields'):
            table['fields'] = [
                {'name': column, 'type': 'inline', 'isKey': False}
                for column in sample['columns']
                if column
            ]

    return tables


def extract_from_binary_qvf(filepath, extract_dir=None):
    """Extract data from binary QVF files (non-ZIP format)."""
    result = {
        'associations': None,
        'metadata': None,
        'script': None,
        'files': [],
        'binaryReport': None,
        'decodedSections': [],
        'undecodedSections': [],
        'evidence': {},
    }

    try:
        with open(filepath, 'rb') as f:
            data = f.read()

        header = data[:4]
        is_binary = header == b'\xff\xff\x01\x00'

        if is_binary:
            text_data = data.decode('utf-8', errors='ignore')
            literal_evidence = _collect_literal_evidence(data)
            scan_result = _scan_gzjson_sections(data)
            decoded_sections = scan_result['decodedSections']
            undecoded_sections = scan_result['undecodedSections']
            decoded_script_candidates = _collect_decoded_script_candidates(decoded_sections)
            inferred_model = infer_binary_model(text_data)
            artifact_info = None

            if decoded_script_candidates:
                result['script'] = "\n\n// --- DECODED FROM COMPRESSED BINARY PAYLOADS ---\n\n".join(
                    candidate['text'] for candidate in decoded_script_candidates
                )
            else:
                candidates = deep_scan_data(data)
                valid_candidates = [candidate for candidate in candidates if looks_like_qlik_script_block(candidate)]
                if valid_candidates:
                    result['script'] = "\n\n// --- EXTRACTED FROM BINARY ---\n\n".join(valid_candidates)

            if inferred_model:
                result['associations'] = inferred_model
                if not result['script']:
                    basename = os.path.basename(filepath)
                    result['script'] = generate_script_from_inferred_model(inferred_model, basename)

            if extract_dir:
                artifact_info = _write_binary_artifacts(
                    filepath,
                    extract_dir,
                    literal_evidence,
                    scan_result,
                    decoded_script_candidates,
                    inferred_model,
                )

            result['decodedSections'] = [
                {
                    key: value for key, value in section.items()
                    if not key.startswith('_')
                }
                for section in decoded_sections
            ]
            result['undecodedSections'] = undecoded_sections
            result['evidence'] = literal_evidence
            result['binaryReport'] = {
                'status': 'partial' if undecoded_sections else 'complete',
                'format': 'binary-qvf',
                'gzjsonMarkerCount': len(scan_result['markerOffsets']),
                'decodedSectionCount': len(decoded_sections),
                'undecodedSectionCount': len(undecoded_sections),
                'scriptSource': (
                    'decoded-sections'
                    if decoded_script_candidates
                    else 'raw-readable-candidates' if result['script']
                    else 'heuristic-reconstruction' if inferred_model
                    else 'none'
                ),
                'associationMode': 'heuristic-inferred' if inferred_model else 'none',
                'artifacts': artifact_info or {},
            }
            result['metadata'] = {
                'binaryReport': result['binaryReport'],
                'decodedSections': result['decodedSections'],
                'undecodedSections': result['undecodedSections'],
                'evidence': result['evidence'],
            }
            _print_binary_terminal_summary(
                filepath,
                result['binaryReport'],
                literal_evidence,
                result['decodedSections'],
                result['undecodedSections'],
                inferred_model,
                result['script'],
            )

        return result
    except Exception as e:
        print(f"ERROR: Binary extraction failed: {e}")
        return result


def extract_qvf(filepath, extract_dir):
    """Extract QVF (ZIP) file and return contents, or read as plain script if not a ZIP."""
    result = {'associations': None, 'metadata': None, 'script': None, 'files': []}
    try:
        if zipfile.is_zipfile(filepath):
            with zipfile.ZipFile(filepath, 'r') as zf:
                file_list = zf.namelist()
                result['files'] = file_list

                zf.extractall(extract_dir)

                assoc_path = next((os.path.join(extract_dir, f) for f in file_list if f.lower() == 'associations.json'), None)
                if assoc_path and os.path.exists(assoc_path):
                    with open(assoc_path, 'r', encoding='utf-8') as f:
                        result['associations'] = json.load(f)

                meta_path = next((os.path.join(extract_dir, f) for f in file_list if f.lower() == 'metadata.json'), None)
                if meta_path and os.path.exists(meta_path):
                    with open(meta_path, 'r', encoding='utf-8') as f:
                        result['metadata'] = json.load(f)

                script_file = next((f for f in file_list if f.lower().endswith('script.qvs')), None)
                if script_file:
                    script_path = os.path.join(extract_dir, script_file)
                    if os.path.exists(script_path):
                        with open(script_path, 'r', encoding='utf-8') as f:
                            result['script'] = f.read()
        else:
            binary_result = extract_from_binary_qvf(filepath, extract_dir=extract_dir)
            if (
                binary_result.get('script')
                or binary_result.get('associations')
                or binary_result.get('binaryReport')
            ):
                return binary_result

            try:
                with open(filepath, 'rb') as f:
                    data = f.read()
                    header = data[:4]
                    if header == b'\xff\xff\x01\x00':
                        raise ValueError("Legacy Binary QVF detected. No readable script blocks found. Please export your script to a .qvs file.")
            except Exception as e:
                if isinstance(e, ValueError):
                    raise e

            content = None
            for encoding in ['utf-8', 'utf-16', 'latin-1']:
                try:
                    with open(filepath, 'r', encoding=encoding, errors='ignore') as f:
                        test_content = f.read()
                        keywords = ['LOAD', 'SELECT', 'SET', 'LET', 'STORE', 'DIRECTORY', 'CONNECT']
                        if any(re.search(rf'\b{kw}\b', test_content, re.IGNORECASE) for kw in keywords):
                            content = test_content
                            break
                except Exception:
                    continue

            if content:
                result['script'] = content
            else:
                raise ValueError("File is not a valid QVF (ZIP archive) and does not appear to be a plain Qlik script.")

    except Exception as e:
        if not isinstance(e, ValueError):
            print(f"ERROR: Extraction error: {str(e)}")
            raise ValueError(f"Error processing QVF file: {str(e)}")
        raise e
    return result


def build_graph_json(all_data, session_files=None):
    nodes = []
    edges = []
    if not all_data:
        return {'nodes': [], 'edges': []}

    uploaded_filenames = set(session_files.values()) if session_files else set()
    for row in all_data:
        file_id = row['file_id']
        filename = session_files.get(file_id, "Unknown") if session_files else "Unknown"
        tables = json.loads(row['tables_json']) if row['tables_json'] else []
        relationships = json.loads(row['associations_json']) if row['associations_json'] else []
        script_text = row['script_text'] or ""

        for table in tables:
            fields = table.get('fields', [])
            key_fields = [f for f in fields if f.get('isKey')]
            table_id = table.get('id') or table.get('name', 'unknown')
            node_id = f"{file_id}_{table_id}"
            nodes.append({
                'id': node_id,
                'name': table.get('name', ''),
                'fileName': filename,
                'fileId': file_id,
                'description': table.get('description', ''),
                'rows': table.get('rows', 0),
                'fields': fields,
                'keyFields': key_fields,
                'fieldCount': len(fields),
                'status': 'uploaded',
                'type': 'fact' if len(key_fields) > 1 else 'dimension'
            })

        table_node_ids = {}
        for table in tables:
            table_id = table.get('id') or table.get('name', 'unknown')
            node_id = f"{file_id}_{table_id}"
            if table.get('id'):
                table_node_ids[str(table.get('id'))] = node_id
            if table.get('name'):
                table_node_ids[str(table.get('name'))] = node_id

        for rel in relationships:
            from_id = rel.get('fromTableId') or rel.get('fromTable')
            to_id = rel.get('toTableId') or rel.get('toTable')
            source_id = table_node_ids.get(str(from_id)) if from_id else None
            target_id = table_node_ids.get(str(to_id)) if to_id else None
            if source_id and target_id:
                edges.append({
                    'id': f"rel_{file_id}_{rel.get('id', str(uuid.uuid4()))}",
                    'source': source_id,
                    'target': target_id,
                    'sourceTable': rel.get('fromTable', ''),
                    'targetTable': rel.get('toTable', ''),
                    'sourceField': rel.get('fromFieldName', ''),
                    'fromFieldName': rel.get('fromFieldName', ''),
                    'toFieldName': rel.get('toFieldName', ''),
                    'relationship': rel.get('relationship', ''),
                    'type': 'internal'
                })

        if script_text:
            pattern = r'\[([^\]]+)\]:\s*LOAD\s+[\s\S]*?FROM\s+\[[^\]]*\/([^\/\]]+\.qvf)\]'
            matches = re.finditer(pattern, script_text, re.IGNORECASE)
            for match in matches:
                target_table_name = match.group(1)
                dep_filename = match.group(2)
                dep_node_id = f"ext_{dep_filename}"
                is_resolved = dep_filename in uploaded_filenames
                if not any(n['id'] == dep_node_id for n in nodes):
                    nodes.append({'id': dep_node_id, 'name': dep_filename, 'status': 'uploaded' if is_resolved else 'missing', 'type': 'external_file', 'description': f"File: {dep_filename}"})
                target_node = next((n for n in nodes if n['name'] == target_table_name and n['fileId'] == file_id), None)
                if target_node:
                    edges.append({'id': f"dep_{dep_node_id}_{target_node['id']}", 'source': dep_node_id, 'target': target_node['id'], 'relationship': 'source_file', 'type': 'dependency'})

    return {'nodes': nodes, 'edges': edges}


def parse_sql_sections(script_text):
    if not script_text:
        return []
    sections = []
    pattern = r'(?:\[([^\]]+)\]|([a-zA-Z0-9_\$]+)):\s*((?:LOAD|SELECT)\s+[\s\S]*?;)'
    matches = re.finditer(pattern, script_text, re.IGNORECASE)
    for match in matches:
        table_name = match.group(1) or match.group(2)
        sections.append({'tableName': table_name, 'sql': match.group(3).strip(), 'fullBlock': match.group(0).strip()})
    return sections


def _normalize_identifier(identifier):
    value = str(identifier or '').strip()
    if not value:
        return None
    return value.strip('[]')


def _parse_script_statements(script_text):
    statement_pattern = re.compile(
        r'(?ims)^\s*'
        r'(?:(?P<prefix>(?:(?:LEFT|RIGHT|INNER|OUTER)\s+)?(?:JOIN|KEEP)|CONCATENATE|NOCONCATENATE|MAPPING)\s*(?:\(\s*(?P<prefix_target>\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*\))?\s*)?'
        r'(?:(?P<label>\[[^\]]+\]|[A-Za-z0-9_\$]+)\s*:\s*)?'
        r'(?P<statement>(?:LOAD|SELECT)\b[\s\S]*?;)',
    )

    statements = []
    for match in statement_pattern.finditer(script_text or ''):
        statements.append({
            'prefix': (match.group('prefix') or '').strip(),
            'label': _normalize_identifier(match.group('label')),
            'prefix_target': _normalize_identifier(match.group('prefix_target')),
            'statement': (match.group('statement') or '').strip(),
            'raw': match.group(0).strip(),
            'start': match.start(),
        })
    return statements


def _split_load_fields(load_body):
    fields = []
    token = []
    depth = 0
    in_single = False
    in_double = False

    for char in load_body:
        if char == "'" and not in_double:
            in_single = not in_single
        elif char == '"' and not in_single:
            in_double = not in_double
        elif not in_single and not in_double:
            if char == '(':
                depth += 1
            elif char == ')' and depth > 0:
                depth -= 1
            elif char == ',' and depth == 0:
                item = ''.join(token).strip()
                if item:
                    fields.append(item)
                token = []
                continue
        token.append(char)

    trailing = ''.join(token).strip()
    if trailing:
        fields.append(trailing)
    return fields


def _extract_field_name(field_expression):
    expr = re.sub(r'//.*$', '', field_expression or '').strip()
    if not expr:
        return None

    alias_match = re.search(r'(?is)\bAS\s+(\[[^\]]+\]|"[^"]+"|`[^`]+`|[A-Za-z0-9_.$]+)\s*$', expr)
    if alias_match:
        alias = alias_match.group(1).strip()
        return alias.strip('[]"`')

    bracket_matches = re.findall(r'\[([^\]]+)\]', expr)
    if bracket_matches:
        return bracket_matches[-1].strip()

    quoted_matches = re.findall(r'"([^"]+)"', expr)
    if quoted_matches:
        return quoted_matches[-1].strip()

    identifier_match = re.search(r'([A-Za-z_][A-Za-z0-9_.$]*)\s*$', expr)
    if identifier_match:
        return identifier_match.group(1).split('.')[-1]

    return None


def _extract_statement_fields(statement):
    sql = statement or ''
    load_match = re.search(r'(?is)\bLOAD\s+(.*?)(?:\bFROM\b|\bRESIDENT\b|\bINLINE\b|;)', sql)
    if load_match:
        field_defs = _split_load_fields(load_match.group(1))
    else:
        select_match = re.search(r'(?is)\bSELECT\s+(.*?)(?:\bFROM\b|;)', sql)
        field_defs = _split_load_fields(select_match.group(1)) if select_match else []

    fields = []
    seen_fields = set()
    for field_def in field_defs:
        field_name = _extract_field_name(field_def)
        if not field_name:
            continue
        normalized_name = field_name.lower()
        if normalized_name in seen_fields:
            continue
        seen_fields.add(normalized_name)
        is_key = (
            normalized_name.endswith('id') or 
            normalized_name.endswith('key') or 
            normalized_name.startswith('%') or
            normalized_name.endswith('_id') or
            normalized_name.endswith('_key')
        )
        fields.append({
            'name': field_name,
            'type': 'unknown',
            'isKey': is_key,
        })
    return fields


def _merge_fields(existing_fields, new_fields):
    merged = list(existing_fields or [])
    seen = {str(field.get('name', '')).lower() for field in merged}
    for field in new_fields or []:
        normalized_name = str(field.get('name', '')).lower()
        if not normalized_name or normalized_name in seen:
            continue
        merged.append(field)
        seen.add(normalized_name)
    return merged


def _extract_dropped_tables(script_text):
    dropped_tables = []
    drop_pattern = re.compile(r'(?ims)\bDROP\s+TABLE\s+(.+?);')
    for match in drop_pattern.finditer(script_text or ''):
        for target in (match.group(1) or '').split(','):
            normalized = _normalize_identifier(target)
            if normalized:
                dropped_tables.append(normalized)
    return dropped_tables


def extract_model_from_script(script_text):
    statements = _parse_script_statements(script_text)
    if not statements:
        return {'tables': [], 'associations': []}

    logical_tables = {}
    table_order = []
    last_materialized_table = None
    pending_chain = None

    def ensure_table(table_name):
        normalized_name = _normalize_identifier(table_name)
        if not normalized_name:
            return None
        if normalized_name not in logical_tables:
            logical_tables[normalized_name] = {
                'id': f"script_{normalized_name}",
                'name': normalized_name,
                'fields': [],
                'rows': 0,
            }
            table_order.append(normalized_name)
        return logical_tables[normalized_name]

    for statement in statements:
        prefix_upper = (statement.get('prefix') or '').upper()
        label = statement.get('label')
        prefix_target = statement.get('prefix_target')
        fields = _extract_statement_fields(statement.get('statement'))

        if label:
            pending_chain = label

        effective_name = label or pending_chain or last_materialized_table
        if not effective_name:
            continue

        merge_target_name = None
        if 'JOIN' in prefix_upper or 'KEEP' in prefix_upper:
            merge_target_name = prefix_target or last_materialized_table
        elif 'CONCATENATE' in prefix_upper and 'NOCONCATENATE' not in prefix_upper:
            merge_target_name = prefix_target or last_materialized_table

        if merge_target_name:
            target_table = ensure_table(merge_target_name)
            if target_table:
                target_table['fields'] = _merge_fields(target_table.get('fields'), fields)
                last_materialized_table = target_table['name']
            pending_chain = None
            continue

        target_table = ensure_table(effective_name)
        if not target_table:
            continue
        target_table['fields'] = _merge_fields(target_table.get('fields'), fields)
        last_materialized_table = target_table['name']

    dropped_tables = {name.lower() for name in _extract_dropped_tables(script_text)}
    tables = []
    field_to_tables = {}

    ASSOCIATION_BLACKLIST = {
        'name', 'description', 'desc', 'comment', 'value', 'amount', 'quantity',
        'date', 'time', 'datetime', 'timestamp', 'year', 'month', 'day',
        'status', 'state', 'type', 'active', 'deleted', 'created_by', 'updated_by',
        'created_at', 'updated_at', 'created_date', 'updated_date', 'row_id',
        'rows', 'fields', 'info', 'flag', 'yesno', 'index', 'number', 'no',
        'f1', 'f2', 'f3', 'f4', 'f5', 'f6', 'f7', 'f8', 'f9', 'f10',
    }

    for table_name in table_order:
        if table_name.lower() in dropped_tables:
            continue
        table = logical_tables[table_name]
        for field in table.get('fields', []):
            normalized_name = str(field.get('name', '')).lower()
            if normalized_name:
                is_explicit_key = field.get('isKey', False)
                if is_explicit_key or (normalized_name not in ASSOCIATION_BLACKLIST):
                    field_to_tables.setdefault(normalized_name, []).append(table_name)
        tables.append(table)

    table_map = {table['name']: table for table in tables}
    associations = []
    seen_pairs = set()
    for field_name, table_names in field_to_tables.items():
        unique_tables = []
        for table_name in table_names:
            if table_name not in unique_tables:
                unique_tables.append(table_name)

        if len(unique_tables) < 2:
            continue

        for index, source_name in enumerate(unique_tables[:-1]):
            for target_name in unique_tables[index + 1:]:
                pair_key = tuple(sorted((source_name.lower(), target_name.lower(), field_name)))
                if pair_key in seen_pairs:
                    continue
                seen_pairs.add(pair_key)
                
                # Dynamically infer relationship cardinality
                rel_cardinality = infer_relationship_cardinality(source_name, target_name, field_name)
                
                associations.append({
                    'id': f"script_{source_name}_{target_name}_{field_name}",
                    'fromTable': source_name,
                    'fromTableId': table_map[source_name]['id'],
                    'fromFieldName': table_map[source_name] and next(
                        (field['name'] for field in table_map[source_name]['fields'] if field['name'].lower() == field_name),
                        field_name,
                    ),
                    'toTable': target_name,
                    'toTableId': table_map[target_name]['id'],
                    'toFieldName': table_map[target_name] and next(
                        (field['name'] for field in table_map[target_name]['fields'] if field['name'].lower() == field_name),
                        field_name,
                    ),
                    'relationship': rel_cardinality,
                })

    return {'tables': tables, 'associations': associations}


def generate_description_rule_based(associations_data, script_text):
    if not associations_data:
        return "No data model available."
    tables = associations_data.get('tables', [])
    relationships = associations_data.get('associations', [])
    lines = ["# Data Model Overview\n"]
    fact_tables = [t for t in tables if len([f for f in t.get('fields', []) if f.get('isKey')]) > 1]
    dim_tables = [t for t in tables if len([f for f in t.get('fields', []) if f.get('isKey')]) <= 1]
    if fact_tables:
        lines.append("## Fact Tables\n")
        for t in fact_tables:
            lines.append(f"**{t['name']}** - {t.get('rows', 0):,} rows\n")
    if dim_tables:
        lines.append("## Dimension Tables\n")
        for t in dim_tables:
            lines.append(f"**{t['name']}** - {t.get('rows', 0):,} rows\n")
    if relationships:
        lines.append("## Relationships\n")
        for r in relationships:
            lines.append(f"- {r['fromTable']} -> {r['toTable']} via {r['fromFieldName']}\n")
    return '\n'.join(lines)
