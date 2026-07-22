#!/usr/bin/env python3
"""Obfuscate/refine and merge asciinema v2 .cast files for a clean demo GIF.

Two subcommands (driven by record_demo.sh):

  demo_cast.py obfuscate IN -o OUT [...]   rewrite one cast: obfuscate text, keep
      timing + cursor codes, and optionally width-truncate, drop sections,
      fast-forward stuck stretches, hold on a marker, force size, prepend a prompt.
  demo_cast.py merge A B C -o OUT [--gap]   concatenate casts into one timeline.

Obfuscation is a SINGLE case-insensitive pass: every rule key is matched
ignoring case and replaced with its value (the matched text's case is discarded).
Only printable text in output ("o") events and the header's string values are
touched - the cursor movements that animate the live dashboard are intact.
Rules come from --rules (default $MAMA_DEMO_RULES or ~/.mama_demo_rules.json).
"""
import argparse, getpass, json, os, re, socket, sys

# Escape sequences (CSI / OSC / two-char) - copied verbatim, never counted as width.
_ESC = re.compile(r'\x1b(?:\[[0-9;?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\)|[@-Z\\-_])')


def truncate_cols(text, n, col):
    """Drop printable characters past visible column `n` on each line, keeping ALL
    control codes (colours, cursor moves, the trailing ESC[K). `col` is the starting
    column carried across events; returns (text, end_col)."""
    out, i, L = [], 0, len(text)
    while i < L:
        m = _ESC.match(text, i)
        if m:
            out.append(m.group(0)); i = m.end(); continue
        c = text[i]; i += 1
        if c == '\n' or c == '\r':
            out.append(c); col = 0
        elif col < n:
            out.append(c); col += 1
    return ''.join(out), col


def signature(text, width=80):
    """A frame fingerprint that ignores volatile numbers (timers, step counts, cpu%)
    and the wide ninja detail past `width`, so two 'same target still building'
    frames compare equal."""
    lines = []
    for line in _ESC.sub('', text).replace('\r', '\n').split('\n'):
        line = re.sub(r'\d+', '#', line[:width])
        line = re.sub(r'\s+', ' ', line).strip()
        if line: lines.append(line)
    return '\n'.join(lines)


# ── rules → one case-insensitive substitution map ────────────────────────────

def load_rules(rules_path, cwd):
    """Return ONE {lowercase-key: replacement} map. Matching is case-insensitive and
    the matched text's case is discarded, so a single pass covers identity, words
    and the kratt catch-all (longest key wins at each position).

    FALLBACK: with no rules file there is nothing to key identity off, so we do the
    one scrub that always matters here - kratt -> drone."""
    if not (rules_path and os.path.exists(rules_path)):
        return {'kratt': 'drone'}

    cfg = json.load(open(rules_path))
    user = cfg.get('user', 'mamabuild'); host = cfg.get('host', 'ubuntu24')
    project = cfg.get('project_display', 'qgroundcontrol')
    home = cfg.get('home') or f'/home/{user}'

    realHome = os.path.expanduser('~'); realProj = os.path.abspath(cwd)
    m = {realProj.lower(): os.path.join(home, project)}
    if realProj.startswith(realHome):
        rel = realProj[len(realHome):].lstrip('/')
        if rel: m['~/' + rel.lower()] = '~/' + project
    m[realHome.lower()] = home
    m[getpass.getuser().lower()] = user
    m[socket.gethostname().lower()] = host
    m.setdefault(os.path.basename(realProj).lower(), project)
    for k, v in cfg.get('replace', {}).items(): m[k.lower()] = v
    for k, v in cfg.get('strip_ci', {'kratt': 'qgc'}).items(): m[k.lower()] = v
    return {k: v for k, v in m.items() if k and k != v.lower()}


def wp_apply(text, pattern, repl_fn):
    """Width-preserving substitution: after each replacement, keep the line width
    constant by consuming (or adding) padding spaces that follow - but only inside a
    run of >=2 spaces, so real column padding is adjusted while single-space
    separators, paths and prose are left as-is."""
    if not pattern:
        return text
    out, i = [], 0
    for m in pattern.finditer(text):
        out.append(text[i:m.start()])
        old = m.group(0); new = repl_fn(old)
        out.append(new)
        j = m.end(); delta = len(new) - len(old)
        run = len(text[j:]) - len(text[j:].lstrip(' '))
        if run >= 2:
            if delta > 0:   j += min(delta, run)
            elif delta < 0: out.append(' ' * (-delta))
        i = j
    out.append(text[i:])
    return ''.join(out)


def make_substitute(rules_path, cwd):
    mapping = load_rules(rules_path, cwd)
    keys = sorted(mapping, key=len, reverse=True)     # longest match wins
    pat = re.compile('|'.join(re.escape(k) for k in keys), re.IGNORECASE) if keys else None
    substitute = lambda s: wp_apply(s, pat, lambda old: mapping[old.lower()])
    return substitute, keys, max((len(k) for k in keys), default=0)


def safe_split(buf, keys, maxkey):
    """(emit, carry): carry = longest suffix of buf that could START a key, held so a
    token straddling two events isn't missed."""
    hold = 0
    for h in range(1, min(len(buf), maxkey) + 1):
        if any(k.startswith(buf[-h:].lower()) for k in keys): hold = h
    return (buf[:len(buf) - hold], buf[len(buf) - hold:]) if hold else (buf, '')


def obf_obj(o, sub):
    if isinstance(o, str):  return sub(o)
    if isinstance(o, dict): return {k: obf_obj(v, sub) for k, v in o.items()}
    if isinstance(o, list): return [obf_obj(v, sub) for v in o]
    return o


def process_drop(text, dropping, start_re, end_re):
    """Cut section(s) from `text` at line boundaries. When `start_re` matches, drop
    from that line on; if `end_re` is given, resume at the line it matches (section
    strip), else stop for good (tail truncation). Returns (kept_text, dropping, stop)."""
    res, pos, stop = [], 0, False
    line_start = lambda s, i: (s.rfind('\n', 0, i) + 1)
    while pos <= len(text):
        if not dropping:
            m = start_re.search(text, pos)
            if not m:
                res.append(text[pos:]); break
            res.append(text[pos:line_start(text, m.start())])
            if end_re is None:
                stop = True; break
            dropping = True; pos = m.end()
        else:
            m = end_re.search(text, pos)
            if not m:
                break
            dropping = False; pos = line_start(text, m.start())
    return ''.join(res), dropping, stop


# ── subcommand: obfuscate ────────────────────────────────────────────────────

def cmd_obfuscate(args):
    substitute, keys, maxkey = make_substitute(args.rules, os.getcwd())
    drop_from_re = re.compile(args.drop_from) if args.drop_from else None
    drop_to_re = re.compile(args.drop_to) if args.drop_to else None
    holds = [(float(s.split(':', 1)[0]), re.compile(s.split(':', 1)[1])) for s in args.hold_at]
    fired = set()

    lines = [ln for ln in open(args.input, encoding='utf-8', errors='replace').read().split('\n') if ln.strip()]
    header = obf_obj(json.loads(lines[0]), substitute)
    if args.cols: header['width'] = args.cols
    if args.rows: header['height'] = args.rows
    out = [json.dumps(header, ensure_ascii=True)]

    col, cur_t = 0, 0.0
    if args.prepend:
        clear = '\x1b[H\x1b[2J\x1b[3J' if args.clear else ''   # home + clear screen + scrollback
        ptext = clear + substitute(args.prepend) + '\r\n'
        if args.max_cols: ptext, col = truncate_cols(ptext, args.max_cols, col)
        out.append(json.dumps([0.0, 'o', ptext], ensure_ascii=True))
        cur_t = 0.4                       # let the prompt sit before output starts

    dropping, carry = False, ''
    prev_real, prev_sig, last_raw = None, None, 0.0
    for ln in lines[1:]:
        ev = json.loads(ln)
        if len(ev) < 3 or ev[1] != 'o':
            out.append(json.dumps([cur_t if args.fast_forward else ev[0]] + ev[1:], ensure_ascii=True))
            continue
        last_raw = ev[0]
        emit, carry = safe_split(carry + ev[2], keys, maxkey)
        text = substitute(emit)
        stop = False
        if drop_from_re:
            text, dropping, stop = process_drop(text, dropping, drop_from_re, drop_to_re)
        if args.max_cols and text:
            text, col = truncate_cols(text, args.max_cols, col)

        if args.fast_forward:
            first = prev_real is None
            real_gap = 0.0 if first else (ev[0] - prev_real)
            prev_real = ev[0]
            sig = signature(text)
            if first:
                delta = 0.4
            elif not sig or (prev_sig is not None and sig == prev_sig):
                delta = args.ff_hold      # stuck: only numbers changed
            else:
                delta = min(real_gap, args.idle_cap)
            if sig: prev_sig = sig
            cur_t = round(cur_t + delta, 4)
            tstamp = cur_t
        else:
            tstamp = ev[0]

        if text:
            out.append(json.dumps([tstamp, 'o', text], ensure_ascii=True))
        if args.fast_forward and text:       # pause after a frame that hits a --hold-at marker
            for idx, (secs, rx) in enumerate(holds):
                if idx not in fired and rx.search(text):
                    fired.add(idx); cur_t = round(cur_t + secs, 4)
        if stop:
            carry = ''; break

    if carry and not dropping:
        text = substitute(carry)
        if args.max_cols: text, col = truncate_cols(text, args.max_cols, col)
        if text:
            out.append(json.dumps([cur_t if args.fast_forward else last_raw, 'o', text], ensure_ascii=True))

    final_t = cur_t if args.fast_forward else last_raw
    if args.end_hold:                     # extend the timeline so the last frame lingers
        final_t = round(final_t + args.end_hold, 4)
        out.append(json.dumps([final_t, 'o', '\x1b[0m'], ensure_ascii=True))

    open(args.output, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    sys.stderr.write(f'[demo_cast] {args.output}: {len(out) - 1} events, ~{final_t:.1f}s\n')


# ── subcommand: merge ────────────────────────────────────────────────────────

def _read_cast(path):
    lines = [ln for ln in open(path, encoding='utf-8', errors='replace').read().split('\n') if ln.strip()]
    return json.loads(lines[0]), [json.loads(ln) for ln in lines[1:]]


def cmd_merge(args):
    """Concatenate casts into one timeline: shift each input by the running offset
    with a gap between segments; the header takes the max width/height."""
    base, _ = _read_cast(args.inputs[0])
    merged = dict(base)
    events, offset = [], 0.0
    for path in args.inputs:
        header, evs = _read_cast(path)
        merged['width'] = max(merged.get('width', 0), header.get('width', 0))
        merged['height'] = max(merged.get('height', 0), header.get('height', 0))
        seg_end = 0.0
        for ev in evs:
            seg_end = max(seg_end, ev[0])
            events.append([round(ev[0] + offset, 3)] + ev[1:])
        offset += seg_end + args.gap
    merged.pop('timestamp', None)
    out = [json.dumps(merged, ensure_ascii=True)] + [json.dumps(e, ensure_ascii=True) for e in events]
    open(args.output, 'w', encoding='utf-8').write('\n'.join(out) + '\n')
    sys.stderr.write(f'[demo_cast] merge {len(args.inputs)} -> {args.output} '
                     f'({merged["width"]}x{merged["height"]}, {round(offset - args.gap, 1)}s)\n')


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest='cmd', required=True)

    o = sub.add_parser('obfuscate', help='obfuscate + refine one cast')
    o.add_argument('input')
    o.add_argument('-o', '--output', required=True)
    o.add_argument('--rules', default=os.environ.get('MAMA_DEMO_RULES',
                   os.path.join(os.path.expanduser('~'), '.mama_demo_rules.json')))
    o.add_argument('--prepend', help='fake prompt+command line drawn before the recording')
    o.add_argument('--clear', action='store_true', help='clear the screen before the prompt')
    o.add_argument('--end-hold', type=float, default=0.0, help='hold the final frame this many seconds')
    o.add_argument('--hold-at', action='append', metavar='SECS:REGEX', default=[],
                   help='pause SECS on the first frame whose text matches REGEX (repeatable; needs --fast-forward)')
    o.add_argument('--drop-from', help='drop from the line matching this regex; to end unless --drop-to is set')
    o.add_argument('--drop-to', help='resume at the line matching this regex (strip a middle section)')
    o.add_argument('--max-cols', type=int, help='truncate visible text past this column')
    o.add_argument('--cols', type=int, help='force header width (columns)')
    o.add_argument('--rows', type=int, help='force header height (rows)')
    o.add_argument('--fast-forward', action='store_true', help='collapse frames where only numbers change')
    o.add_argument('--ff-hold', type=float, default=0.05, help='seconds given to a collapsed (unchanged) frame')
    o.add_argument('--idle-cap', type=float, default=0.5, help='cap the real gap of a changed frame to this')
    o.set_defaults(func=cmd_obfuscate)

    m = sub.add_parser('merge', help='concatenate casts into one timeline')
    m.add_argument('inputs', nargs='+')
    m.add_argument('-o', '--output', required=True)
    m.add_argument('--gap', type=float, default=1.2, help='seconds of pause between segments')
    m.set_defaults(func=cmd_merge)

    args = ap.parse_args()
    args.func(args)


if __name__ == '__main__':
    main()
