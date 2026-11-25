"""Account for labelled main-paper figures and tables using frozen text and declared scope."""
import argparse
import hashlib
import json
from pathlib import Path
import re


CAPTION = re.compile(r'^\s*(?:#{1,6}\s+)?(Figure|Fig\.?|Table)\s+([1-9]\d{0,2})\s*(?:[.:]\s+|\|\s*)\S', re.I)
PLAIN_LABEL = re.compile(r'^\s*(?:#{1,6}\s+)?(Figure|Fig\.?|Table)\s+([1-9]\d{0,2})(?:\s+(.*)|[.:]?)$', re.I)
CITATION = re.compile(r'\b(Figures?|Figs?\.?|Tables?)\s+([1-9]\d{0,2})(?:[a-z])?\b', re.I)


def identifier(kind, number):
    return ('Table ' if kind.casefold().startswith('tab') else 'Figure ') + number


def primary_text(record):
    kind = record.get('kind')
    name = record.get('snapshot_path')
    return kind in {'paper_text', 'article_text'} or (
        kind in {'paper', 'article'} and isinstance(name, str) and Path(name).suffix.lower() == '.txt')


def caption_headings(lines):
    """Use paragraph boundaries and title typography for unpunctuated labels."""
    previous, current = '', next(lines, '')
    line_number = 1
    while current:
        following = next(lines, '')
        text = current.strip()
        explicit = CAPTION.match(text)
        if explicit:
            yield line_number, identifier(*explicit.groups()), text
        elif (label := PLAIN_LABEL.match(text)) and (
                not previous.strip() or previous.rstrip().endswith(('.', '!', '?'))):
            title = (label[3] or following).strip()
            first = title.split(maxsplit=1)[0] if title else ''
            # A wrapped prose reference commonly continues with a lower-case
            # verb, preposition or punctuation. A caption commonly opens with
            # a title-cased noun or a mixed-case scientific abbreviation.
            if (len(title.split()) >= 2 and first[0].isalpha() and any(c.isupper() for c in first)
                    and not PLAIN_LABEL.match(title) and not CAPTION.match(title)):
                yield line_number, identifier(label[1], label[2]), text if label[3] else text + ' ' + title
        previous, current = current, following
        line_number += 1


def captions(bundle, manifest):
    result, seen = [], set()
    bundle = Path(bundle).resolve()
    for record in manifest.get('artifacts', []):
        if record.get('status') != 'available' or not primary_text(record):
            continue
        name = record.get('snapshot_path')
        if not isinstance(name, str) or Path(name).is_absolute():
            continue
        path = (bundle/name).resolve()
        identity = record.get('sha256') or name
        if identity in seen or not path.is_relative_to(bundle) or not path.is_file() or path.stat().st_size > 64 * 1024 * 1024:
            continue
        seen.add(identity)
        with path.open(encoding='utf-8', errors='replace') as stream:
            for number, label, excerpt in caption_headings(stream):
                result.append({'id': label, 'path': name, 'line': number, 'excerpt': excerpt[:240]})
    return result


def evidence_lines(ref, paths, documents):
    """Check the stated excerpt at its stated lines in a frozen, trusted text."""
    if not isinstance(ref, dict):
        return None
    name = ref.get('path')
    if not isinstance(name, str) or name not in paths:
        return None
    start, end, quote = ref.get('line_start'), ref.get('line_end'), ref.get('quote')
    if type(start) is not int or type(end) is not int or not isinstance(quote, str) or not quote.strip():
        return None
    path, expected = paths[name]
    if name not in documents:
        raw = path.read_bytes()
        # Caption inventory line numbers are defined by physical ``\\n`` records
        # in the frozen text artifact.  ``str.splitlines()`` also treats form
        # feed and other Unicode control characters as line breaks, shifting
        # every later caption in PDF-to-text exports.  Keep the same line model
        # as ``captions()``/file iteration so frozen evidence remains stable.
        documents[name] = (hashlib.sha256(raw).hexdigest(), raw.decode(errors='replace').split('\n'))
    digest, lines = documents[name]
    if ref.get('sha256') != digest or (expected and expected != digest) or not 1 <= start <= end <= len(lines):
        return None
    excerpt = ' '.join(' '.join(lines[start-1:end]).split())
    if ' '.join(quote.split()) not in excerpt:
        return None
    return name, start, end


def _search_text(value):
    import unicodedata
    return ''.join(c for c in unicodedata.normalize('NFKD', value).casefold() if c.isalnum())


def find_source_quotes(bundle, *, phrase=None, source_path=None, line_range=None, limit=5):
    """Return exact, line-addressed excerpts from verified primary paper text."""
    from bisect import bisect_right
    bundle = Path(bundle).resolve(strict=True)
    manifest = json.loads((bundle/'materials_manifest.json').read_text())
    query = _search_text(phrase or '')
    if line_range is None and not query:
        raise ValueError('supply a word or phrase from the primary paper')
    if line_range is not None and not source_path:
        raise ValueError('select --source-path for --quote-lines')
    matches = []
    seen = set()
    for item in manifest.get('artifacts', []):
        name = item.get('snapshot_path')
        if (item.get('status') != 'available' or not primary_text(item) or not isinstance(name, str)
                or source_path is not None and name != source_path):
            continue
        path = (bundle/name).resolve()
        if Path(name).is_absolute() or not path.is_relative_to(bundle) or path.stat().st_size > 64*1024*1024:
            raise ValueError('expected a bounded primary text inside the frozen bundle')
        data = path.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if digest != item.get('sha256'):
            raise ValueError('frozen primary text has changed: ' + name)
        if digest in seen:
            continue
        seen.add(digest)
        lines = data.decode('utf-8').splitlines()
        def cite(first,last,quote):
            return {'path':name, 'sha256':digest, 'line_start':first, 'line_end':last,
                    'locator':f'lines {first}–{last}', 'quote':quote}
        if line_range is not None:
            first,last = line_range
            if not 1 <= first <= last <= len(lines):
                raise ValueError('requested lines are outside the selected text')
            quote = '\n'.join(lines[first-1:last]).strip()
            if not quote:
                raise ValueError('selected lines have no text')
            return [cite(first,last,quote)]
        pieces = [_search_text(line) for line in lines]
        offsets = [0]
        for piece in pieces:
            offsets.append(offsets[-1]+len(piece))
        corpus = ''.join(pieces)
        pos = corpus.find(query)
        previous_lines = None
        while pos >= 0:
            start = bisect_right(offsets,pos)-1
            end = bisect_right(offsets,pos+len(query)-1)-1
            if (start,end) != previous_lines:
                excerpt = '\n'.join(lines[start:end+1])
                # Preserve original punctuation and ligatures even for a long HTML line.
                if len(excerpt) > 650:
                    target_start = pos - offsets[start]
                    target_stop = target_start + len(query)
                    count, left, right = 0, None, None
                    for i,char in enumerate(excerpt):
                        count += len(_search_text(char))
                        if left is None and count > target_start:
                            left = i
                        if count >= target_stop:
                            right = i+1
                            break
                    excerpt = excerpt[max(0,(left or 0)-100):(right or len(excerpt))+100]
                matches.append(cite(start+1,end+1,excerpt.strip()))
                if len(matches) == limit:
                    return matches
                previous_lines = start,end
            pos = corpus.find(query,pos+len(query))
    if source_path is not None and not seen:
        raise ValueError('source path is absent from the frozen primary text inventory')
    return matches


def v2_decision(refs, decision, experiments, quality, primary, boundary, documents):
    if not isinstance(decision, dict) or not isinstance(decision.get('rationale'), str) or not decision['rationale'].strip():
        return 'supply your own figure_table_decisions entry and scientific rationale'
    status = decision.get('status')
    if status not in ('covered', 'duplicate', 'schematic', 'outside_scope'):
        return 'choose covered, duplicate, schematic or outside_scope and support the decision with source evidence'
    caption = None
    if status == 'covered' and decision.get('caption_id') == refs[0]['id'] and 'caption_evidence' not in decision:
        # The decisions map is reviewed separately. Reuse the host's caption address.
        pass
    else:
        caption = evidence_lines(decision.get('caption_evidence'), primary, documents)
        if not caption or not any(caption[0] == r['path'] and caption[1] == r['line'] for r in refs):
            return 'cite the indexed caption by its frozen source and line; a covered result may instead use caption_id'
    if status in ('covered', 'duplicate'):
        ids = decision.get('experiment_ids')
        if not isinstance(ids, list) or not ids:
            return 'map this result to at least one scored main experiment'
        for eid in ids:
            exp = experiments.get(eid) if isinstance(eid, str) else None
            reqs = exp.get('requirement_ids') if isinstance(exp, dict) else None
            if not isinstance(reqs, list) or not any(isinstance(q, str) and q in quality for q in reqs):
                return 'each mapped main experiment must bind an existing quality requirement'
    if status == 'outside_scope':
        if not evidence_lines(decision.get('boundary_evidence'), boundary, documents):
            return 'supply the exact exclusion in the frozen fixed task instruction as boundary_evidence'
    if status == 'duplicate' or (status == 'schematic' and 'body_evidence' in decision):
        evidence = evidence_lines(decision.get('body_evidence'), primary, documents)
        if not evidence or (evidence[0] == caption[0] and evidence[1] <= caption[2] and caption[1] <= evidence[2]):
            return 'supply a distinct body_evidence excerpt explaining the illustration or repeated result'
    return None


def duplicate_comparison(current, decisions, inventory, experiments, quality, primary, documents):
    """A repeated result must identify its covered display and leave a source-bound scientific comparison."""
    decision = decisions[current]
    original = decision.get('duplicate_of')
    other = decisions.get(original) if isinstance(original, str) and original in inventory else None
    if current == original or not isinstance(other, dict) or other.get('status') != 'covered':
        return 'duplicate_of must name a different indexed display independently marked covered'
    original_ids = other.get('experiment_ids')
    if not isinstance(original_ids, list):
        return 'duplicate_of must identify a scored experiment in its covered display'
    common = set(decision['experiment_ids']).intersection(e for e in original_ids if isinstance(e, str))
    comparison = decision.get('same_result')
    requirement = comparison.get('requirement_id') if isinstance(comparison, dict) else None
    if not isinstance(requirement, str) or requirement not in quality or not any(
            requirement in experiments[e].get('requirement_ids', []) for e in common):
        return 'bind same_result.requirement_id to a shared scored experiment in both displays'
    proof = comparison.get('source_evidence')
    if not isinstance(proof,dict):
        return 'supply the current and original displays’ source_evidence for the outcome, population and contrast'
    texts = {}
    for side, display in [('current',current),('original',original)]:
        citation = proof.get(side)
        span = evidence_lines(citation,primary,documents)
        if not span:
            return f'supply the frozen paper excerpt describing the {side} result'
        mentioned = {identifier(k,n) for k,n in CITATION.findall(citation['quote'])}
        if display not in mentioned and not any(span[0]==r['path'] and span[1]==r['line'] for r in inventory[display]):
            return f'the {side} result excerpt must cite its display or start at its caption'
        texts[side] = ' '.join(citation['quote'].split()).casefold()
    matching = True
    for field in ('outcome','population','contrast'):
        values = comparison.get(field)
        if not isinstance(values,dict):
            return f'compare the {field} in both original source excerpts'
        for side, source in texts.items():
            term = values.get(side)
            if not isinstance(term,str) or not term.strip() or ' '.join(term.split()).casefold() not in source:
                return f'the {side} {field} must quote its cited source excerpt'
        matching &= ' '.join(values['current'].split()).casefold() == ' '.join(values['original'].split()).casefold()
    if not matching:
        equivalence = comparison.get('equivalence_evidence')
        if not evidence_lines(equivalence, primary, documents) or not {current, original}.issubset(
                {identifier(k,n) for k,n in CITATION.findall(equivalence['quote'])}):
            return 'different source terms require a frozen equivalence_evidence excerpt connecting both displays'
    return None


def audit(bundle, manifest, protocol, review=None, *, version=1):
    candidates = captions(bundle, manifest)
    by_id = {}
    for item in candidates:
        by_id.setdefault(item['id'], []).append(item)
    scope = protocol.get('assessment', {}).get('scope', {})
    experiments = scope.get('main_experiments', {}) if isinstance(scope, dict) else {}
    if not isinstance(experiments, dict):
        experiments = {}
    primary_paths = {r.get('snapshot_path') for r in manifest.get('artifacts', [])
                     if r.get('status') == 'available' and r.get('kind') in
                     {'article', 'paper', 'paper_text', 'article_text', 'article_pdf', 'article_xml', 'article_html'}}
    linked = {}
    for eid, experiment in experiments.items():
        if not isinstance(experiment, dict) or not isinstance(experiment.get('source_evidence'), list):
            continue
        for ref in experiment['source_evidence']:
            if not isinstance(ref, dict) or not isinstance(ref.get('path', ref.get('snapshot_path')), str):
                continue
            if ref.get('path', ref.get('snapshot_path')) not in primary_paths:
                continue
            keys = {identifier(kind, number) for kind, number in CITATION.findall(str(ref.get('locator', '')))}
            quoted = next(caption_headings(iter(str(ref.get('quote', '')).splitlines(keepends=True)[:2])), None)
            if quoted and quoted[0] == 1:
                keys.add(quoted[1])
            for key in keys:
                if key in by_id:
                    linked.setdefault(key, set()).add(eid)
    verdicts = review if review is not None else scope
    explanations = verdicts.get('figure_table_decisions', {}) if isinstance(verdicts, dict) else {}
    rows, errors = [], []
    if version in (2,3):
        bundle = Path(bundle).resolve()
        primary = {}
        for record in manifest.get('artifacts', []):
            name = record.get('snapshot_path')
            if not isinstance(name, str) or not primary_text(record) or record.get('status') != 'available':
                continue
            path = (bundle / name).resolve()
            if not Path(name).is_absolute() and path.is_relative_to(bundle) and path.is_file() and path.stat().st_size <= 64*1024*1024:
                primary[name] = (path, record.get('sha256'))
        fixed = bundle / 'fixed_task_instruction.md'
        boundary = {fixed.name: (fixed, None)} if fixed.is_file() and fixed.resolve().is_relative_to(bundle) else {}
        documents = {}
        requirements = protocol.get('assessment', {}).get('requirements')
        quality = {r['id'] for r in requirements if isinstance(r, dict) and r.get('role')=='quality' and isinstance(r.get('id'), str)} if isinstance(requirements, list) else set()
    for key, refs in by_id.items():
        matched = sorted(linked.get(key, set()))
        entry = {'id': key, 'sources': refs, 'experiment_ids': matched}
        if version in (2,3):
            decision = explanations.get(key) if isinstance(explanations, dict) else None
            problem = v2_decision(refs, decision, experiments, quality, primary, boundary, documents)
            if not problem and version==3 and decision['status']=='duplicate':
                problem=duplicate_comparison(key,explanations,by_id,experiments,quality,primary,documents)
            if problem:
                entry['status'] = 'unaccounted'
                errors.append(f"{key} ({refs[0]['path']}:{refs[0]['line']}): {problem}")
            else:
                entry.update(status=decision['status'], rationale=decision['rationale'], experiment_ids=decision.get('experiment_ids', []))
        elif matched:
            entry['status'] = 'cited'
        else:
            decision = explanations.get(key, {}) if isinstance(explanations, dict) else {}
            valid = isinstance(decision, dict) and isinstance(decision.get('rationale'), str) and bool(decision['rationale'].strip())
            status = decision.get('status') if valid else None
            if status == 'covered':
                ids = decision.get('experiment_ids')
                valid = isinstance(ids, list) and bool(ids) and all(isinstance(i, str) and i in experiments for i in ids)
            else:
                valid = status in {'non_experimental', 'outside_scope'}
            if valid:
                entry.update(status=status, rationale=decision['rationale'])
                if status == 'covered':
                    entry['experiment_ids'] = decision['experiment_ids']
            else:
                entry['status'] = 'unaccounted'
                errors.append(f"{key} ({refs[0]['path']}:{refs[0]['line']}): cite a main experiment or explain this caption in figure_table_decisions")
        rows.append(entry)
    return {'phase': 'review' if review is not None else 'construction', 'candidates': rows, 'errors': errors}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sources', type=Path, required=True)
    parser.add_argument('--protocol', type=Path, required=True)
    parser.add_argument('--review-report', type=Path)
    parser.add_argument('--version', type=int, choices=(1, 2, 3), help='Defaults to the package execution contract version, or 1 for legacy standalone protocols')
    args = parser.parse_args()
    review = None
    if args.review_report:
        review = json.loads(args.review_report.read_text()).get('scientific_checks', {}).get('scope_fidelity', {})
    version = args.version
    if version is None:
        execution = args.protocol.resolve().parents[2] / 'execution.json' if len(args.protocol.resolve().parents) >= 3 else None
        version = json.loads(execution.read_text()).get('paper_caption_review_version', 1) if execution and execution.is_file() else 1
    result = audit(args.sources, json.loads((args.sources/'materials_manifest.json').read_text()),
                   json.loads(args.protocol.read_text()), review, version=version)
    result['source_bundle'] = str(args.sources.resolve())
    result['protocol'] = {'path': str(args.protocol.resolve()),
                          'sha256': hashlib.sha256(args.protocol.read_bytes()).hexdigest()}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return int(bool(result['errors']))


if __name__ == '__main__':
    raise SystemExit(main())
