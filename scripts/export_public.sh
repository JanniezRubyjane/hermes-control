#!/usr/bin/env bash
# Export the committed HEAD tree into a fresh repository; never push it.
# Private paths, relative to the repository root (also exclude descendants):
EXCLUDES=(docs/dev/ .hermes.md .worktrees/ scripts/pstack_cite_check.py)
set -euo pipefail

[ "$#" -eq 1 ] || { echo "usage: scripts/export_public.sh DEST" >&2; exit 2; }
unset GIT_DIR GIT_WORK_TREE GIT_INDEX_FILE GIT_COMMON_DIR
export GIT_CONFIG_GLOBAL=/dev/null GIT_CONFIG_NOSYSTEM=1
repo="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
dest="$1"
[ ! -L "$dest" ] || { echo "export_public: DEST must not be a symlink" >&2; exit 2; }
if [ -e "$dest" ]; then
    [ -d "$dest" ] || { echo "export_public: DEST must be a directory" >&2; exit 2; }
    [ -z "$(find "$dest" -mindepth 1 -maxdepth 1 -print -quit)" ] || {
        echo "export_public: refusing non-empty DEST" >&2; exit 2;
    }
fi
scratch="$(mktemp -d "${TMPDIR:-/tmp}/hc-export-public.XXXXXX")"
trap 'rm -rf "$scratch"' EXIT
# Archive first so a missing HEAD cannot leave a half-created destination.
head="$(git -C "$repo" rev-parse --verify HEAD)"
objects="$(git -C "$repo" rev-parse --path-format=absolute --git-path objects)"
# A disposable object view suppresses export-ignore/export-subst attributes so
# the result is the raw HEAD tree, not a source archive with missing files or
# expanded private commit metadata. Source Git configuration is not copied.
git init -q --bare --template= "$scratch/archive.git"
mkdir -p "$scratch/archive.git/info"
printf '%s\n' '* -export-ignore -export-subst' > "$scratch/archive.git/info/attributes"
GIT_ALTERNATE_OBJECT_DIRECTORIES="$objects" git --git-dir="$scratch/archive.git" \
    archive --format=tar --output="$scratch/tree.tar" "$head"
mkdir -p "$scratch/tree"
tar -xf "$scratch/tree.tar" -C "$scratch/tree"
# List the committed tree with a disposable index, so neither uncommitted source
# edits nor archive attributes affect the selected paths. Match root-relative
# names here: tar exclude patterns also match nested public paths.
GIT_ALTERNATE_OBJECT_DIRECTORIES="$objects" GIT_INDEX_FILE="$scratch/index" \
    git --git-dir="$scratch/archive.git" read-tree "$head"
GIT_INDEX_FILE="$scratch/index" git --git-dir="$scratch/archive.git" \
    --work-tree="$scratch/tree" ls-files -z > "$scratch/paths"
while IFS= read -r -d '' path; do
    excluded=false
    for exclude in "${EXCLUDES[@]}"; do
        root_path="${exclude%/}"
        if [[ "$path" == "$root_path" || "$path" == "$root_path/"* ]]; then
            excluded=true
            break
        fi
    done
    if ! "$excluded"; then printf '%s\0' "$path"; fi
done < "$scratch/paths" > "$scratch/public-paths"
tar -cf "$scratch/public.tar" -C "$scratch/tree" --null --no-recursion \
    -T "$scratch/public-paths"
mkdir -p "$dest"
dest="$(cd "$dest" && pwd)"
tar -xf "$scratch/public.tar" -C "$dest"
# Disable local templates, hooks and signing; do not inherit the operator's identity.
git -C "$dest" init -q --template= --initial-branch=main
git -C "$dest" add -f --all
export GIT_AUTHOR_NAME=JanniezRubyjane
export GIT_AUTHOR_EMAIL=273864440+JanniezRubyjane@users.noreply.github.com
export GIT_COMMITTER_NAME="$GIT_AUTHOR_NAME"
export GIT_COMMITTER_EMAIL="$GIT_AUTHOR_EMAIL"
export GIT_AUTHOR_DATE="$(date -u '+%Y-%m-%d') 00:00:00 +0000"
export GIT_COMMITTER_DATE="$GIT_AUTHOR_DATE"
git -C "$dest" -c core.hooksPath=/dev/null -c commit.gpgsign=false \
    commit -q -m 'First public release'

# Split the search literals so this scanner does not flag its own source.
pattern='/''Users/|/''home/|@''gmail|sk-''ant-|oauth/''usage'
status=0
git -C "$dest" -c core.quotePath=false grep --full-name -a -n -i -E "$pattern" HEAD -- > "$scratch/hits" || status=$?
[ "$status" -le 1 ] || { echo "export_public: scan failed" >&2; exit "$status"; }
# Private markers are local configuration, not part of the exported tree.
# Preserve each literal line, including spaces and regex punctuation.
if [ -f "$repo/docs/dev/leak-markers.txt" ]; then
    LC_ALL=C awk '{ sub(/\r$/, "") } !/^[[:space:]]*#/ && /[^[:space:]]/' \
        "$repo/docs/dev/leak-markers.txt" > "$scratch/markers"
    if [ -s "$scratch/markers" ]; then
        status=0
        git -C "$dest" -c core.quotePath=false grep --full-name -a -n -i -F \
            -f "$scratch/markers" HEAD -- >> "$scratch/hits" || status=$?
        [ "$status" -le 1 ] || { echo "export_public: scan failed" >&2; exit "$status"; }
    fi
else
    echo 'export_public: no private marker file; generic scan only' >&2
fi
# U+0E00-U+0E7F as UTF-8 bytes in text only: Bash 3.2 and Git without PCRE.
status=0
LC_ALL=C git -C "$dest" -c core.quotePath=false grep --full-name -I -n -E \
    $'\340\270[\200-\277]|\340\271[\200-\277]' HEAD -- >> "$scratch/hits" || status=$?
[ "$status" -le 1 ] || { echo "export_public: scan failed" >&2; exit "$status"; }
# HEAD: prefix is removed for a reviewable file:line report. The sole exception
# is the existing fake key fixture at exactly tests/test_planner_status.py:7.
sed 's/^HEAD://' "$scratch/hits" | \
    awk -F: '!($1 == "tests/test_planner_status.py" && $2 == "7")' > "$scratch/leaks"
if [ -s "$scratch/leaks" ]; then
    cat "$scratch/leaks"
    echo "export_public: scan found private data; do not publish DEST" >&2
    exit 1
fi
echo "export_public: one commit exported; scan clean; nothing pushed"
