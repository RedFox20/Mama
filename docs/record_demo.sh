#!/usr/bin/env bash
# Record mama's live build dashboard for each demo command, obfuscate + refine it
# (timing and cursor codes preserved), merge into one timeline, and render a single
# animated GIF. Self-contained: the two files (record_demo.sh, demo_cast.py) can
# be moved to ~/Mama/docs/ as-is.
#
# ── INSTALL (one-time) ───────────────────────────────────────────────────────
#   sudo apt install -y asciinema
#   curl -Lo ~/.local/bin/agg \
#     https://github.com/asciinema/agg/releases/latest/download/agg-x86_64-unknown-linux-gnu
#   chmod +x ~/.local/bin/agg
#
# ── USE ──────────────────────────────────────────────────────────────────────
#   cd <project to demo>          # mama commands run here
#   ~/Mama/docs/record_demo.sh                # record + render  -> demo.gif
#   RENDER_ONLY=1 ...record_demo.sh           # re-render from existing casts (no rebuild)
#
# Obfuscation rules live OUTSIDE this dir so it can be published without leaking
# real names: $MAMA_DEMO_RULES, default ~/.mama_demo_rules.json.
#
# Tunables (env): SPEED FPS FONT GAP MAXCOLS FF_HOLD IDLE THEME COLS_ENV ROWS_ENV
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="${PROJECT_DIR:-$PWD}"
RULES="${MAMA_DEMO_RULES:-$HOME/.mama_demo_rules.json}"
OUT="${OUT:-$HERE}"; CASTS="$OUT/casts"; GIF="$OUT/demo.gif"; MERGED="$OUT/demo_merged.cast"
mkdir -p "$CASTS"

SPEED="${SPEED:-1}"        # extra uniform agg speed-up (ff already compresses)
FPS="${FPS:-15}"           # agg frame cap
FONT="${FONT:-14}"
THEME="${THEME:-asciinema}"
GAP="${GAP:-1.0}"          # pause between segments
MAXCOLS="${MAXCOLS:-120}"  # truncate wide ninja detail + bound GIF width
HEIGHT="${HEIGHT:-28}"     # GIF rows (max live region is ~14, so this is safe)
FF_HOLD="${FF_HOLD:-0.012}"
IDLE="${IDLE:-0.35}"
COLS_ENV="${COLS_ENV:-120}"; ROWS_ENV="${ROWS_ENV:-$HEIGHT}"   # terminal size for NEW recordings
RENDER_ONLY="${RENDER_ONLY:-0}"

STEPS=(
  "prep:mama clean all"
  "rec:mama build"
  "prep:mama android clean all"
  "rec:mama android build"
  "prep:mama clang clean all"
  "rec:mama clang build buildstats rtpvideo"
)

# preflight ---------------------------------------------------------------------
miss=0
for t in asciinema agg; do command -v "$t" >/dev/null || { echo "!! '$t' not installed"; miss=1; }; done
if [ "$miss" = 1 ]; then cat <<'EOF'
Install:
  sudo apt install -y asciinema
  curl -Lo ~/.local/bin/agg https://github.com/asciinema/agg/releases/latest/download/agg-x86_64-unknown-linux-gnu
  chmod +x ~/.local/bin/agg
EOF
  exit 1; fi
[ -f "$RULES" ] || echo "!! rules not found: $RULES  - fallback obfuscation: kratt->drone only (identity NOT scrubbed)"

# coloured fake prompt from the identity in the rules file (defaults if no file) -
read -r U H P < <(python3 - "$RULES" <<'PY'
import json,os,sys
r = json.load(open(sys.argv[1])) if os.path.exists(sys.argv[1]) else {}
print(r.get('user','mamabuild'), r.get('host','ubuntu24'), r.get('project_display','qgroundcontrol'))
PY
)
G=$'\033[32m'; B=$'\033[34m'; R=$'\033[0m'
PROMPT="${G}${U}@${H}${R}:${B}~/${P}${R}$ "

cd "$PROJECT_DIR"
export MAMA_DEMO_RULES="$RULES"
OBFCASTS=()
for step in "${STEPS[@]}"; do
  kind="${step%%:*}"; cmd="${step#*:}"
  name="mama_$(echo "${cmd#mama }" | tr ' ' '_')"
  raw="$CASTS/$name.cast"; obf="$CASTS/$name.obf.cast"

  if [ "$kind" = prep ]; then
    [ "$RENDER_ONLY" = 1 ] && continue
    echo ">>> prep (not recorded): $cmd"
    eval "$cmd" >/dev/null 2>&1 || echo "    (prep exited $? - continuing)"
    continue
  fi

  if [ "$RENDER_ONLY" != 1 ]; then
    echo ">>> record: $cmd"
    COLUMNS="$COLS_ENV" LINES="$ROWS_ENV" asciinema rec --overwrite --quiet -c "$cmd" "$raw"
  fi
  [ -f "$raw" ] || { echo "    !! no cast $raw (record first, or unset RENDER_ONLY)"; continue; }

  # keep Build Insights after the warnings block; everything else drops to end.
  # buildstats also gets a 3s hold so the insights are readable.
  case "$cmd" in
    *buildstats*) DROP=(--drop-from 'Compiler diagnostics' --drop-to 'Build Insights')
                  HOLD=(--hold-at "${HOLD_BUILT:-1}:Built [0-9]+ target" --end-hold 3) ;;
    *)            DROP=(--drop-from 'Compiler diagnostics'); HOLD=() ;;
  esac
  echo ">>> refine: $cmd  ->  $obf"
  python3 "$HERE/demo_cast.py" obfuscate "$raw" -o "$obf" --prepend "${PROMPT}${cmd}" --clear \
    --max-cols "$MAXCOLS" --cols "$MAXCOLS" --rows "$HEIGHT" \
    --fast-forward --ff-hold "$FF_HOLD" --idle-cap "$IDLE" "${DROP[@]}" "${HOLD[@]}"
  OBFCASTS+=("$obf")
done

[ "${#OBFCASTS[@]}" -gt 0 ] || { echo "!! nothing to render"; exit 1; }
echo ">>> merge ${#OBFCASTS[@]} segments"
python3 "$HERE/demo_cast.py" merge "${OBFCASTS[@]}" -o "$MERGED" --gap "$GAP"

echo ">>> render -> $GIF  (speed ${SPEED}x, ${FPS}fps, font ${FONT})"
agg --speed "$SPEED" --fps-cap "$FPS" --font-size "$FONT" --theme "$THEME" "$MERGED" "$GIF"

echo ">>> leftover-secret scan:"
python3 - "$CASTS" "$RULES" <<'PY'
import json, os, re, sys, glob
keys = list(json.load(open(sys.argv[2])).get('strip_ci', {})) or ['kratt']
pat = re.compile('|'.join(re.escape(k) for k in keys), re.IGNORECASE)
bad = False
for f in sorted(glob.glob(os.path.join(sys.argv[1], '*.obf.cast'))):
    n = len(pat.findall(open(f, encoding='utf-8', errors='replace').read()))
    if n: bad = True; print(f"   !! {f}: {n} leftover hit(s)")
print("   clean." if not bad else "   ^ add the word(s) to the rules file and re-run.")
PY
echo ">>> done: $GIF  ($(du -h "$GIF" | cut -f1))"
