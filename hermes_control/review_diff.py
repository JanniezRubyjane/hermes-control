"""Read-only structured committed diffs; never expose git diagnostics."""
from __future__ import annotations

import codecs
from pathlib import Path
import re
import subprocess

from . import runs, worktree
from .review_pack import _resolve_git_evidence
from .secret_scan import _RULES

MAX_FILES = 300
MAX_LINES_PER_FILE = 3000
MAX_BYTES = 1_000_000
_HUNK = re.compile(r'^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@')
# Catch PEM/OpenSSH bodies when a diff hunk omits both key delimiters.
_KEY_BODY = re.compile(r'\s*(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])'
                       r'(?=[A-Za-z0-9+/]*[0-9])[A-Za-z0-9+/]{40,76}={0,2}\s*')
# Only standalone END markers justify backfilling; code can quote their parts.
_KEY_END = re.compile(r'\s*-----END (?:(?:[A-Z0-9]+ )*PRIVATE KEY|PGP PRIVATE KEY BLOCK)-----\s*')
# Fixed a/ and b/ prefixes: the section parser keys on them, and a repo's own
# diff.noprefix / diff.srcPrefix / diff.dstPrefix would otherwise fail every diff.
_DIFF_FLAGS = ('--no-ext-diff', '--no-textconv', '--no-color', '--no-renames',
               '--src-prefix=a/', '--dst-prefix=b/')


def _git(repo, *args):
    result = worktree._run_git(args, cwd=str(repo))
    if result.returncode:
        raise ValueError('git diff failed')
    return result.stdout


def _mask(text):
    masked = []
    for name, pattern in _RULES:
        text, count = pattern.subn(lambda match: f'«masked:{name}»', text)
        if count:
            masked.append(name)
    return text, masked


def _unquote_path(path):
    if path.startswith('"'):
        if not re.fullmatch(r'"(?:[^"\\]|\\.)*"', path):
            raise ValueError('git diff failed')
        # 'replace' matches how _run_git decodes the numstat paths it is compared with.
        return codecs.escape_decode(path[1:-1].encode('utf-8'))[0].decode('utf-8', 'replace')
    return path


def _section_path(section):
    header = section.split('\n', 1)[0].removeprefix('diff --git ')
    candidates = set()
    # Unquoted paths may contain spaces, including " b/". Only accept a split
    # whose decoded old/new paths agree; --no-renames makes that mandatory.
    for match in re.finditer(' ', header):
        try:
            old = _unquote_path(header[:match.start()])
            new = _unquote_path(header[match.end():])
        except (ValueError, UnicodeError):
            continue
        if old.startswith('a/') and new.startswith('b/') and old[2:] == new[2:]:
            candidates.add(old[2:])
    if len(candidates) != 1:
        raise ValueError('git diff failed')
    path = candidates.pop()
    metadata = section.split('\n@@ ', 1)[0]
    for marker in re.finditer(r'^(---|\+\+\+) (.*)$', metadata, re.M):
        value = _unquote_path(marker[2].split('\t', 1)[0])
        prefix = 'a/' if marker[1] == '---' else 'b/'
        if value not in ('/dev/null', prefix + path):
            raise ValueError('git diff failed')
    return path


def _parse(patch, stats, result):
    sections = re.split(r'(?=^diff --git )', patch, flags=re.MULTILINE)
    by_path = {row[0]: [] for row in stats}
    if len(by_path) != len(stats):
        raise ValueError('git diff failed')
    for section in sections:
        if not section:
            continue
        path = _section_path(section)
        if path not in by_path:
            raise ValueError('git diff failed')
        by_path[path].append(section)
    # Validate even entries beyond the display limits before returning any files.
    if any(not matched for matched in by_path.values()):
        raise ValueError('git diff failed')
    used_bytes = 0
    for index, (path, added, removed, binary) in enumerate(stats):
        if index >= MAX_FILES:
            result['truncated'] = True
            break
        section = ''.join(by_path[path])
        metadata = section.split('\n', 1)[1].split('\n@@ ', 1)[0]
        status = ('other' if len(by_path[path]) > 1 else
                  'added' if metadata.startswith('new file mode ') else
                  'deleted' if metadata.startswith('deleted file mode ') else
                  'mode' if 'old mode ' in metadata and not re.search(r'^@@ ', section, re.M) else
                  'modified' if binary or re.search(r'^@@ ', section, re.M) else 'other')
        file = dict(path=path, status=status, binary=binary, added=added,
                    removed=removed, truncated=False, hunks=[])
        result['files'].append(file)
        if binary:
            continue
        hunk = None
        old = new = line_count = 0
        private_key = False

        def mask_line(text, *, body=False):
            nonlocal private_key, used_bytes
            if body and not private_key and _KEY_END.fullmatch(text):
                # BEGIN may be outside the hunk; only backfill this hunk.
                for previous in hunk['lines']:
                    result['secret_hits'] += not previous['masked']
                    if 'private-key' not in previous['masked']:
                        previous['masked'].append('private-key')
                    used_bytes += (len('«masked:private-key»'.encode('utf-8')) -
                                   len(previous['text'].encode('utf-8')))
                    previous['text'] = '«masked:private-key»'
                private_key = True
            masked_text, masked = _mask(text)
            if 'private-key' in masked:
                private_key = True
            if private_key or (body and _KEY_BODY.fullmatch(text)):
                masked_text = '«masked:private-key»'
                if 'private-key' not in masked:
                    masked.append('private-key')
                if (('-----END' in text and 'PRIVATE KEY-----' in text) or
                        _KEY_END.fullmatch(text)):
                    private_key = False
            result['secret_hits'] += bool(masked)
            return masked_text, masked

        for raw in section.removesuffix('\n').split('\n'):
            if raw.startswith('diff --git '):
                hunk = None
                continue
            match = _HUNK.match(raw)
            if match:
                old, new = map(int, match.groups())
                # Headers can include source text (git's function context).
                header, _ = mask_line(raw)
                hunk = dict(header=header, lines=[])
                file['hunks'].append(hunk)
                continue
            if hunk is None or (raw and raw[0] not in '+- \\'):
                continue
            if line_count >= MAX_LINES_PER_FILE:
                file['truncated'] = result['truncated'] = True
                break
            prefix = raw[0] if raw else ' '
            kind = {'+': 'add', '-': 'del', ' ': 'ctx', '\\': 'note'}[prefix]
            text, masked = mask_line(raw if kind == 'note' else raw[1:], body=True)
            line = dict(kind=kind, old=old if kind in ('del', 'ctx') else None,
                        new=new if kind in ('add', 'ctx') else None, text=text, masked=masked)
            hunk['lines'].append(line)
            line_count += 1
            old += kind in ('del', 'ctx')
            new += kind in ('add', 'ctx')
            used_bytes += len(text.encode('utf-8')) + 64
            if used_bytes > MAX_BYTES:
                file['truncated'] = result['truncated'] = True
                return


def build(run_id: str, task_id: str) -> dict | None:
    """Return only a task selected from the run log, with masked patch text."""
    view = runs.run_view(run_id)
    if view is None:
        return None
    task = next((task for task in view['tasks'] if task['id'] == task_id), None)
    if task is None:
        return None
    result = dict(run_id=run_id, task_id=task_id, branch=task['branch'] or None,
                  commit=None, base=None, error=None, files=[], added=0, removed=0,
                  truncated=False, secret_hits=0,
                  limits=dict(max_files=MAX_FILES, max_lines_per_file=MAX_LINES_PER_FILE,
                              max_bytes=MAX_BYTES))
    try:
        if view['repo'] and Path(view['repo']).is_dir():
            _git(Path(view['repo']), 'rev-parse', '--git-dir')
        evidence = _resolve_git_evidence(view['repo'], task)
        if 'git_error' in evidence:
            message = evidence['git_error']
            result['error'] = next((error for error in ('repo gone', 'branch gone',
                                   'commit not recorded') if message.startswith(error)),
                                   'git diff failed')
            return result
        repo = Path(view['repo'])
        result['commit'] = _git(repo, 'rev-parse', '--verify',
                                f"{evidence['commit']}^{{commit}}").strip()
        result['base'] = _git(repo, 'rev-parse', '--verify',
                              f"{evidence['base_sha']}^{{commit}}").strip()
        span = f"{result['base']}..{result['commit']}"
        numstat = _git(repo, 'diff', *_DIFF_FLAGS, '--numstat', '-z', span)
        stats = []
        for record in numstat.split('\0'):
            if not record:
                continue
            add, remove, path = record.split('\t', 2)
            stats.append((path, int(add) if add != '-' else 0,
                          int(remove) if remove != '-' else 0, add == '-'))
        result['added'] = sum(row[1] for row in stats)
        result['removed'] = sum(row[2] for row in stats)
        patch = _git(repo, 'diff', *_DIFF_FLAGS, span)
        _parse(patch, stats, result)
    except (OSError, subprocess.SubprocessError, ValueError, RuntimeError):
        result.update(error='git diff failed', files=[])
    return result
