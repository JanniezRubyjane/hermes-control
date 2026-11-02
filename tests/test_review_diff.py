"""Committed dashboard diffs use isolated repositories and run logs."""
import importlib.util
from pathlib import Path
import subprocess

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_control import review_diff
from hermes_control.events import RawEvent, stamp
from hermes_control.runlog import RunLog


def git(repo, *args):
    return subprocess.run(['git', '-C', str(repo), *args], check=True,
                          capture_output=True, text=True).stdout.strip()


@pytest.fixture
def change(tmp_path):
    repo = tmp_path / 'repo'
    repo.mkdir()
    git(repo, 'init', '-q')
    git(repo, 'config', 'user.email', 'test@example.com')
    git(repo, 'config', 'user.name', 'Test')
    for name, content in [('modified', 'keep\nold\n'), ('deleted', 'gone\n')]:
        (repo / name).write_text(content)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'base')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'checkout', '-qb', 'task-branch')
    (repo / 'modified').write_text('keep\nnew\n')
    (repo / 'deleted').unlink()
    for name in ['space name', '雪.txt', 'quote"\tname']:
        (repo / name).write_text('first\nlast')
    (repo / 'binary').write_bytes(b'\x00binary')

    def record(commit=True, recorded_base=True, base_override=None):
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'change', '--allow-empty')
        sha = git(repo, 'rev-parse', 'HEAD')
        log = RunLog('diff-run')
        events = [('', 'plan', {'repo': str(repo), 'tasks': [{'id': 'task', 'engine': 'codex'}]})]
        if commit:
            events.append(('task', 'commit', {'sha': sha, 'branch': 'task-branch'}))
        events.append(('', 'summary', {'tasks': [{'task_id': 'task', 'branch': 'task-branch',
                       **({'base_commit': base_override or base} if recorded_base else {})}]}))
        for task, kind, payload in events:
            log.append(stamp([RawEvent(kind=kind, payload=payload)], run_id='diff-run',
                             task_id=task, engine='codex', start_seq=log.next_seq(task), now=1790000000))
        return sha
    return repo, base, record


def lines(file):
    return [line for hunk in file['hunks'] for line in hunk['lines']]


def test_structured_diff(change):
    repo, base, record = change
    sha = record()
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    assert (result['base'], result['commit']) == (base, sha)
    files = {f['path']: f for f in result['files']}
    assert files['binary']['binary'] and files['binary']['hunks'] == []
    assert files['deleted']['status'] == 'deleted'
    assert files['space name']['status'] == 'added'
    assert '雪.txt' in files and 'quote"\tname' in files
    assert [(x['kind'], x['old'], x['new'], x['text']) for x in lines(files['modified'])] == [
        ('ctx', 1, 1, 'keep'), ('del', 2, None, 'old'), ('add', None, 2, 'new')]
    assert lines(files['space name'])[-1] == {
        'kind': 'note', 'old': None, 'new': None,
        'text': '\\ No newline at end of file', 'masked': []}
    assert all(x['masked'] == [] for f in files.values() for x in lines(f))
    for name in ['space name', '雪.txt', 'quote\"\tname']:
        assert [x['text'] for x in lines(files[name]) if x['kind'] == 'add'] == (
            repo / name).read_text().splitlines()
    assert result['secret_hits'] == 0
    assert result['added'] == 7 and result['removed'] == 2


def test_textconv_never_executes(change, tmp_path):
    repo, _, record = change
    marker = tmp_path / 'executed'
    (repo / '.gitattributes').write_text('* diff=evil\n')
    git(repo, 'config', 'diff.evil.textconv', f'touch {marker}')
    git(repo, 'config', 'diff.evil.command', f'touch {marker}')
    record()
    assert review_diff.build('diff-run', 'task')['error'] is None
    assert not marker.exists()


@pytest.mark.parametrize('kind', ['add', 'del', 'ctx'])
def test_masks_all_sides(change, kind):
    repo, _, record = change
    token = 'ghp_' + 'a' * 36
    if kind != 'add':
        (repo / 'modified').write_text(token + '\nold\n')
        git(repo, 'add', '.')
        git(repo, 'commit', '-qm', 'secret base')
    secret_base = git(repo, 'rev-parse', 'HEAD') if kind != 'add' else None
    (repo / 'modified').write_text((token + '\nnew\n') if kind != 'del' else 'new\n')
    record(base_override=secret_base)
    result = review_diff.build('diff-run', 'task')
    assert token not in str(result)
    assert result['secret_hits'] == 1
    assert any(x['masked'] == ['github-token'] for f in result['files'] for x in lines(f))


@pytest.mark.parametrize('limit,value', [('MAX_FILES', 2), ('MAX_LINES_PER_FILE', 1), ('MAX_BYTES', 3)])
def test_limits_preserve_totals(change, monkeypatch, limit, value):
    _, _, record = change
    record()
    monkeypatch.setattr(review_diff, limit, value)
    result = review_diff.build('diff-run', 'task')
    assert result['truncated']
    assert result['added'] == 7 and result['removed'] == 2
    if limit == 'MAX_FILES':
        assert len(result['files']) == value
    if limit == 'MAX_LINES_PER_FILE':
        assert any(f['truncated'] for f in result['files'])
        assert all(len(lines(f)) <= value for f in result['files'])


@pytest.mark.parametrize('problem,error', [('branch', 'branch gone'), ('commit', 'commit not recorded'),
                                         ('repo', 'repo gone'), ('git', 'git diff failed')])
def test_errors(change, problem, error):
    repo, _, record = change
    record(commit=problem != 'commit')
    if problem == 'branch':
        git(repo, 'checkout', '--detach')
        git(repo, 'branch', '-D', 'task-branch')
    elif problem == 'repo':
        repo.rename(repo.with_name('moved'))
    elif problem == 'git':
        (repo / '.git').rename(repo / 'gone-git')
    result = review_diff.build('diff-run', 'task')
    assert result['error'] == error and result['files'] == []


def test_missing_and_route(change, monkeypatch):
    from hermes_cli.dashboard_auth import prefix
    monkeypatch.setattr(prefix, 'resolve_public_url', lambda: '')
    _, _, record = change
    record()
    assert review_diff.build('missing', 'task') is None
    assert review_diff.build('diff-run', 'unknown') is None
    assert review_diff.build('..', 'task') is None
    path = Path(__file__).resolve().parents[1] / 'hermes_control/dashboard/plugin_api.py'
    spec = importlib.util.spec_from_file_location('hc_diff_api', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        url = '/runs/diff-run/tasks/task/diff'
        assert client.get(url).status_code == 200
        assert client.get(url, headers={'Origin': 'http://localhost:5173'}).status_code == 403
        for url in ['/runs/missing/tasks/task/diff', '/runs/diff-run/tasks/unknown/diff']:
            response = client.get(url)
            assert response.status_code == 404
            assert response.json()['detail'] == 'no such run or task'


def test_merge_base_mode_and_read_only(change):
    repo, base, record = change
    (repo / 'modified').chmod(0o755)
    sha = record(recorded_base=False)
    git(repo, 'checkout', '--detach', base)
    before = (git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'))
    dashboard = Path(__file__).resolve().parents[1] / 'hermes_control/dashboard'
    snapshot = {p: p.read_bytes() for p in dashboard.rglob('*') if p.is_file()}
    result = review_diff.build('diff-run', 'task')
    assert result['base'] == base and result['commit'] == sha
    assert before == (git(repo, 'status', '--porcelain'), git(repo, 'rev-parse', 'HEAD'))
    assert snapshot == {p: p.read_bytes() for p in dashboard.rglob('*') if p.is_file()}
    assert set(result) == {'run_id', 'task_id', 'branch', 'commit', 'base', 'error', 'files',
                           'added', 'removed', 'truncated', 'secret_hits', 'limits'}
    assert result['limits'] == dict(max_files=300, max_lines_per_file=3000, max_bytes=1000000)


def test_mode_only(change):
    repo, _, record = change
    (repo / 'deleted').write_text('gone\n')
    (repo / 'deleted').chmod(0o755)
    record()
    file = next(f for f in review_diff.build('diff-run', 'task')['files'] if f['path'] == 'deleted')
    assert file == dict(path='deleted', status='mode', binary=False, added=0, removed=0,
                        truncated=False, hunks=[])


def test_git_stderr_is_never_exposed(change, monkeypatch, caplog):
    _, _, record = change
    record()
    original = review_diff.worktree._run_git
    def fail_diff(args, **kwargs):
        if args[0] == 'diff':
            return subprocess.CompletedProcess(args, 1, '', 'PRIVATE FILE CONTENT')
        return original(args, **kwargs)
    monkeypatch.setattr(review_diff.worktree, '_run_git', fail_diff)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] == 'git diff failed' and result['files'] == []
    assert 'PRIVATE FILE CONTENT' not in str(result) + caplog.text


@pytest.mark.parametrize('name', ['aaa-type', 'aaa space', 'aaa"\t雪'])
def test_type_change_keeps_sections_with_their_path(change, name):
    repo, _, record = change
    (repo / name).write_text('regular content\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'type base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / name).unlink()
    (repo / name).symlink_to('target')
    (repo / 'modified').write_text('keep\nlater file content\n')
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    files = {f['path']: f for f in result['files']}
    assert files[name]['status'] == 'other'
    assert len(files[name]['hunks']) == 2
    assert [x['text'] for x in lines(files[name]) if x['kind'] != 'note'] == [
        'regular content', 'target']
    assert [x['text'] for x in lines(files['modified'])] == [
        'keep', 'new', 'later file content']


@pytest.mark.parametrize('missing', ['section', 'record'])
def test_unmatched_diff_fails_closed(change, monkeypatch, missing):
    _, _, record = change
    record()
    original = review_diff._git
    def altered(repo, *args):
        output = original(repo, *args)
        if args[0] == 'diff':
            if missing == 'record' and '--numstat' in args:
                return output.split('\0', 1)[1]
            if missing == 'section' and '--numstat' not in args:
                return 'diff --git '.join(output.split('diff --git ')[0:1] +
                                         output.split('diff --git ')[2:])
        return output
    monkeypatch.setattr(review_diff, '_git', altered)
    result = review_diff.build('diff-run', 'task')
    assert result['files'] == [] and result['error'] == 'git diff failed'


def test_private_key_block_masks_every_kind_until_end(change):
    repo, _, record = change
    (repo / 'modified').write_text('-----BEGIN ' 'RSA PRIVATE KEY-----\nOLDFAKEBODY\n'
                                   'sharedfakebody\n-----END RSA PRIVATE KEY-----\nafter\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'key base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'modified').write_text('-----BEGIN ' 'RSA PRIVATE KEY-----\nNEWFAKEBODY\n'
                                   'sharedfakebody\n-----END RSA PRIVATE KEY-----\nafter\n')
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert all(x['masked'] == ['private-key'] for x in lines(file)[:-1])
    assert lines(file)[-1]['text'] == 'after' and lines(file)[-1]['masked'] == []
    assert 'FAKEBODY' not in str(result) and 'sharedfakebody' not in str(result)
    assert result['secret_hits'] == 5


def test_partial_key_end_masks_every_line_and_counts_once(change):
    repo, _, record = change
    body = [('AbCdEfGh0123+/' * 5 + str(i))[-64:] for i in range(12)]
    before = '-----BEGIN ' 'RSA PRIVATE KEY-----\n' + '\n'.join(body)
    ending = '\n-----END RSA PRIVATE KEY-----\nafter\n'
    (repo / 'modified').write_text(before + ending)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'partial key base')
    base = git(repo, 'rev-parse', 'HEAD')
    body[-1] = 'shortfakebody'
    (repo / 'modified').write_text('-----BEGIN ' 'RSA PRIVATE KEY-----\n' +
                                   '\n'.join(body) + ending)
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert len(file['hunks']) == 1
    assert file['hunks'][0]['header'].startswith('@@ -10,')
    assert all(x['text'] == '«masked:private-key»' and x['masked'] == ['private-key']
               for x in lines(file)[:-1])
    assert lines(file)[-1]['text'] == 'after' and lines(file)[-1]['masked'] == []
    assert result['secret_hits'] == len(lines(file)) - 1


def test_partial_key_body_without_delimiters(change):
    repo, _, record = change
    body = [('AbCdEfGh0123+/' * 5 + str(i))[-64:] for i in range(30)]
    begin = '-----BEGIN ' 'RSA PRIVATE KEY-----\n'
    ending = '\n-----END RSA PRIVATE KEY-----\nafter\n'
    (repo / 'modified').write_text(begin + '\n'.join(body) + ending)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'partial key base')
    base = git(repo, 'rev-parse', 'HEAD')
    body[15] = ' \t' + ('IjKlMnOp4567+/' * 5)[:64] + '== \t'
    (repo / 'modified').write_text(begin + '\n'.join(body) + ending)
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert len(file['hunks']) == 1 and len(lines(file)) == 8
    assert all(x['text'] == '«masked:private-key»' and x['masked'] == ['private-key']
               for x in lines(file))
    assert all('AbCdEfGh' not in x['text'] and 'IjKlMnOp' not in x['text']
               for f in result['files'] for x in lines(f))
    assert result['secret_hits'] == len(lines(file))


def test_partial_key_end_backfill_preserves_earlier_hunk_and_hits(change):
    repo, _, record = change
    token = 'ghp_' + 'a' * 36
    begin = '-----BEGIN ' 'RSA PRIVATE KEY-----\n'
    before = 'old\n' + 'ordinary code\n' * 12 + begin + 'shortfakebody\n' * 12
    ending = token + '\noldbody\n-----END RSA PRIVATE KEY-----\nafter\n'
    (repo / 'modified').write_text(before + ending)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'partial key base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'modified').write_text(before.replace('old\n', 'new\n', 1) +
                                   ending.replace('oldbody', 'newbody'))
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert len(file['hunks']) == 2
    assert all(x['masked'] == [] for x in file['hunks'][0]['lines'])
    key_lines = file['hunks'][1]['lines'][:-1]
    assert all(x['text'] == '«masked:private-key»' and
               x['masked'].count('private-key') == 1 for x in key_lines)
    assert any(x['masked'] == ['github-token', 'private-key'] for x in key_lines)
    assert lines(file)[-1]['text'] == 'after' and lines(file)[-1]['masked'] == []
    assert result['secret_hits'] == len(key_lines)


def test_partial_key_lowercase_sha_and_code_stay_visible(change):
    repo, _, record = change
    content = 'abcdef0123' * 4 + '\nvalue = 123\nreturn value\n'
    (repo / 'modified').write_text(content)
    record()
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert all(x['masked'] == [] for x in lines(file))
    assert [x['text'] for x in lines(file) if x['kind'] == 'add'] == content.splitlines()
    assert result['secret_hits'] == 0


def test_key_end_anchor_leaves_code_visible(change):
    repo, _, record = change
    content = ("before = 1\nif '-----END' in text and "
               "'PRIVATE KEY-----' in text:\n    private_key = False\nafter = 2\n")
    (repo / 'modified').write_text(content)
    record()
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert all(x['masked'] == [] for x in lines(file))
    assert [x['text'] for x in lines(file) if x['kind'] == 'add'] == content.splitlines()
    assert result['secret_hits'] == 0


def test_key_end_anchor_pgp_backfills_and_resets(change):
    repo, _, record = change
    begin = '-----BEGIN ' 'PGP PRIVATE KEY BLOCK-----\n'
    body = ['shortfakebody'] * 12
    ending = '\n-----END ' 'PGP PRIVATE KEY BLOCK-----\nafter\n'
    (repo / 'modified').write_text(begin + '\n'.join(body) + ending)
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'partial PGP key base')
    base = git(repo, 'rev-parse', 'HEAD')
    body[-1] = 'newshortfakebody'
    (repo / 'modified').write_text(begin + '\n'.join(body) + ending)
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert len(file['hunks']) == 1
    assert file['hunks'][0]['header'].startswith('@@ -10,')
    assert all(x['text'] == '«masked:private-key»' and x['masked'] == ['private-key']
               for x in lines(file)[:-1])
    assert lines(file)[-1]['text'] == 'after' and lines(file)[-1]['masked'] == []
    assert result['secret_hits'] == len(lines(file)) - 1


@pytest.mark.parametrize(('token', 'matches'), [
    (('AbCd0123' * 10)[:40], True),
    (('AbCd0123' * 10)[:76], True),
    (('AbCd0123' * 10)[:40] + '==', True),
    (('AbCd0123' * 10)[:76] + '==', True),
    (('AbCd0123' * 10)[:39], False),
    (('AbCd0123' * 10)[:77], False),
    ('AbCdEfGh' * 8, False),
    ('ABCD0123' * 8, False),
    ('abcd0123' * 8, False),
    ('AbCd0123' * 8 + '===', False),
])
def test_key_body_bounds(token, matches):
    assert bool(review_diff._KEY_BODY.fullmatch(token)) is matches


def test_many_short_lines_count_json_overhead(change):
    repo, _, record = change
    for i in range(6):
        (repo / f'short-{i}').write_text('x\n' * 2900)
    record()
    result = review_diff.build('diff-run', 'task')
    assert result['truncated']
    count = sum(len(lines(f)) for f in result['files'])
    assert count < 16000


def test_masked_hunk_header_counts_as_secret(change):
    repo, _, record = change
    token = 'ghp_' + 'b' * 36
    (repo / 'modified').write_text(token + '\n' + '  keep\n' * 20 + 'old\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'header base')
    base = git(repo, 'rev-parse', 'HEAD')
    (repo / 'modified').write_text(token + '\n' + '  keep\n' * 20 + 'new\n')
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    assert token not in str(result)
    assert any('«masked:github-token»' in h['header']
               for f in result['files'] for h in f['hunks'])
    assert result['secret_hits'] == 1


def test_suppressed_blank_context_keeps_line_numbers(change):
    repo, _, record = change
    (repo / 'modified').write_text('keep\n\nold\n')
    git(repo, 'add', '.')
    git(repo, 'commit', '-qm', 'blank base')
    base = git(repo, 'rev-parse', 'HEAD')
    git(repo, 'config', 'diff.suppressBlankEmpty', 'true')
    (repo / 'modified').write_text('keep\n\nnew\n')
    record(base_override=base)
    result = review_diff.build('diff-run', 'task')
    file = next(f for f in result['files'] if f['path'] == 'modified')
    assert [(x['kind'], x['old'], x['new'], x['text']) for x in lines(file)] == [
        ('ctx', 1, 1, 'keep'), ('ctx', 2, 2, ''),
        ('del', 3, None, 'old'), ('add', None, 3, 'new')]


@pytest.mark.parametrize('settings', [
    {'diff.noprefix': 'true'},
    {'diff.srcPrefix': 'left/', 'diff.dstPrefix': 'right/'},
])
def test_repo_diff_prefix_settings_do_not_break_the_diff(change, settings):
    repo, _, record = change
    for key, value in settings.items():
        git(repo, 'config', key, value)
    record()
    result = review_diff.build('diff-run', 'task')
    assert result['error'] is None
    assert any(f['path'] == 'modified' and f['hunks'] for f in result['files'])
