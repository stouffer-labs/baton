#!/usr/bin/env python3
"""Fixture tests for bin/baton: discovery, classification, titles, children,
the Codex state DB, sanitization, the persistent cache, and live detection.

Each test builds a throwaway fake HOME under the repo's gitignored tmp/ and
runs bin/baton as a subprocess against it (HOME, XDG_CACHE_HOME, BATON_STATE
point inside that HOME). Process introspection is replaced by
BATON_TEST_PROCS so the host's real claude/codex processes never leak in, and
every run starts in a new session (no controlling terminal) so a baton that
doesn't understand a flag can never open the interactive picker.

Run from the repo root:  python3 -m unittest discover -s tests -v
"""
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import tempfile
import time
import unittest
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BATON = REPO / 'bin' / 'baton'
if not os.access(str(BATON), os.X_OK):
    # git here (core.filemode=false) and the GitHub contents API don't keep the
    # exec bit; the tests run bin/baton directly, as the installer does.
    BATON.chmod(BATON.stat().st_mode | 0o111)
TMP_ROOT = REPO / 'tmp'
TIMEOUT = int(os.environ.get('BATON_TEST_TIMEOUT', '60'))

LSTART = 'Sun Sep 27 02:56:37 2026'
CONTROL_RE = re.compile(r'[\x00-\x1f\x7f-\x9f]')
CLAUDE_BIN = '/opt/claude-code/2.1.283/claude'
CODEX_BIN = '/opt/codex/0.157.1/codex'

# Verbatim from ~/.codex/state_5.sqlite (Codex 0.157).
CODEX_THREADS_SQL = """CREATE TABLE threads (
    id TEXT PRIMARY KEY,
    rollout_path TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    source TEXT NOT NULL,
    model_provider TEXT NOT NULL,
    cwd TEXT NOT NULL,
    title TEXT NOT NULL,
    sandbox_policy TEXT NOT NULL,
    approval_mode TEXT NOT NULL,
    tokens_used INTEGER NOT NULL DEFAULT 0,
    has_user_event INTEGER NOT NULL DEFAULT 0,
    archived INTEGER NOT NULL DEFAULT 0,
    archived_at INTEGER,
    git_sha TEXT,
    git_branch TEXT,
    git_origin_url TEXT
, cli_version TEXT NOT NULL DEFAULT '', first_user_message TEXT NOT NULL DEFAULT '', agent_nickname TEXT, agent_role TEXT, memory_mode TEXT NOT NULL DEFAULT 'enabled', model TEXT, reasoning_effort TEXT, agent_path TEXT, created_at_ms INTEGER, updated_at_ms INTEGER, thread_source TEXT, preview TEXT NOT NULL DEFAULT '', recency_at INTEGER NOT NULL DEFAULT 0, recency_at_ms INTEGER NOT NULL DEFAULT 0, history_mode TEXT NOT NULL DEFAULT 'legacy', name TEXT, is_pinned INTEGER NOT NULL DEFAULT 0, thread_section_id TEXT
    REFERENCES thread_sections(id) ON DELETE SET NULL, section_position INTEGER, section_entered_at_ms INTEGER, project_id TEXT
    REFERENCES projects(id) ON DELETE SET NULL, originator TEXT, daybreak_enabled BOOLEAN, creator_user_id TEXT, creator_account_id TEXT)"""

CODEX_EDGES_SQL = """CREATE TABLE thread_spawn_edges (
    parent_thread_id TEXT NOT NULL,
    child_thread_id TEXT NOT NULL PRIMARY KEY,
    status TEXT NOT NULL
)"""

CODEX_AUX_SQL = ("CREATE TABLE thread_sections (id TEXT PRIMARY KEY);"
                 "CREATE TABLE projects (id TEXT PRIMARY KEY);")

KINDS = ('root', 'automated', 'child', 'superseded', 'empty')


def new_uuid():
    return str(uuid.uuid4())


def slug_for(cwd):
    """Claude's project-dir slug: every non-alphanumeric char becomes '-'."""
    return re.sub(r'[^A-Za-z0-9]', '-', cwd)


def norm_ws(s):
    return ' '.join((s or '').split())


# ---------------------------------------------------------------- Claude records

def c_user(sid, cwd, content, entrypoint='cli', **extra):
    d = {'parentUuid': None, 'isSidechain': False, 'promptId': new_uuid(),
         'type': 'user', 'message': {'role': 'user', 'content': content},
         'uuid': new_uuid(), 'timestamp': '2026-09-27T10:00:00.000Z',
         'permissionMode': 'default', 'promptSource': 'typed',
         'userType': 'external'}
    if entrypoint is not None:
        d['entrypoint'] = entrypoint
    d.update({'cwd': cwd, 'sessionId': sid, 'version': '2.1.283', 'gitBranch': 'main'})
    d.update(extra)
    return d


def c_assistant(sid, cwd, text, entrypoint='cli', **extra):
    d = {'parentUuid': new_uuid(), 'isSidechain': False, 'type': 'assistant',
         'message': {'role': 'assistant', 'type': 'message',
                     'content': [{'type': 'text', 'text': text}]},
         'uuid': new_uuid(), 'timestamp': '2026-09-27T10:00:05.000Z',
         'userType': 'external'}
    if entrypoint is not None:
        d['entrypoint'] = entrypoint
    d.update({'cwd': cwd, 'sessionId': sid, 'version': '2.1.283', 'gitBranch': 'main'})
    d.update(extra)
    return d


def c_tool_result(sid, cwd, entrypoint='cli'):
    return c_user(sid, cwd, [{'type': 'tool_result', 'tool_use_id': 'toolu_01',
                              'content': 'command output'}], entrypoint=entrypoint,
                  toolUseResult='ok')


def c_attachment(sid, cwd, nbytes, entrypoint='cli'):
    d = {'parentUuid': new_uuid(), 'isSidechain': False, 'type': 'attachment',
         'attachment': {'type': 'file', 'content': 'a' * nbytes},
         'uuid': new_uuid(), 'timestamp': '2026-09-27T10:00:01.000Z',
         'userType': 'external'}
    if entrypoint is not None:
        d['entrypoint'] = entrypoint
    d.update({'cwd': cwd, 'sessionId': sid, 'version': '2.1.283'})
    return d


def c_system(sid, cwd, entrypoint='cli'):
    return {'parentUuid': None, 'isSidechain': False, 'type': 'system',
            'subtype': 'init', 'content': 'session start', 'uuid': new_uuid(),
            'timestamp': '2026-09-27T10:00:00.000Z', 'userType': 'external',
            'entrypoint': entrypoint, 'cwd': cwd, 'sessionId': sid,
            'version': '2.1.283'}


def c_last_prompt(sid, text):
    return {'type': 'last-prompt', 'lastPrompt': text, 'leafUuid': new_uuid(),
            'sessionId': sid}


def c_ai_title(sid, t):
    return {'type': 'ai-title', 'aiTitle': t, 'sessionId': sid}


def c_custom_title(sid, t):
    return {'type': 'custom-title', 'customTitle': t, 'sessionId': sid}


def c_agent_name(sid, t):
    return {'type': 'agent-name', 'agentName': t, 'sessionId': sid}


def c_summary(t):
    return {'type': 'summary', 'summary': t, 'leafUuid': new_uuid()}


def c_continued_in(sid, successor):
    return {'type': 'continued-in', 'timestamp': '2026-09-25T22:24:51.165Z',
            'sessionId': sid, 'continuedInSessionId': successor}


def c_basic(sid, cwd, prompt='hello there', entrypoint='cli', title=None, **user_extra):
    """A small, realistic interactive transcript."""
    recs = [{'type': 'queue-operation', 'operation': 'enqueue',
             'timestamp': '2026-09-27T10:00:00.000Z', 'sessionId': sid,
             'content': prompt},
            c_user(sid, cwd, prompt, entrypoint=entrypoint, **user_extra),
            c_assistant(sid, cwd, 'sure, working on it', entrypoint=entrypoint),
            c_last_prompt(sid, prompt)]
    if title:
        recs.append(c_ai_title(sid, title))
    return recs


BUILTIN_MODEL_CMD = ('<command-name>/model</command-name>\n'
                     '            <command-message>model</command-message>\n'
                     '            <command-args></command-args>')


# ---------------------------------------------------------------- base case

class BatonCase(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        TMP_ROOT.mkdir(exist_ok=True)
        self.home = Path(tempfile.mkdtemp(prefix='test-home-', dir=str(TMP_ROOT)))
        self.projects = self.home / '.claude' / 'projects'
        self.projects.mkdir(parents=True)
        self.codex = self.home / '.codex'
        (self.codex / 'sessions').mkdir(parents=True)
        self.procs = []

    def tearDown(self):
        shutil.rmtree(self.home, ignore_errors=True)

    # --- environment / running baton

    def work(self, name):
        p = self.home / 'work' / name
        p.mkdir(parents=True, exist_ok=True)
        return str(p)

    def env(self):
        procs_file = self.home / 'procs.json'
        procs_file.write_text(json.dumps(self.procs))
        e = {'HOME': str(self.home),
             'XDG_CACHE_HOME': str(self.home / '.cache'),
             'BATON_STATE': str(self.home / 'state'),
             'BATON_TEST_PROCS': str(procs_file),
             'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
             'LANG': os.environ.get('LANG', 'en_US.UTF-8'),
             'TERM': 'dumb'}
        return e

    def run_baton(self, *args, check=True):
        try:
            p = subprocess.run([str(BATON)] + list(args), env=self.env(),
                               cwd=str(self.home), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True, timeout=TIMEOUT,
                               start_new_session=True)
        except subprocess.TimeoutExpired:
            self.fail('baton %s timed out after %ss' % (' '.join(args), TIMEOUT))
        if check and p.returncode != 0:
            self.fail('baton %s exited %d\n--- stdout ---\n%s\n--- stderr ---\n%s'
                      % (' '.join(args), p.returncode, p.stdout[-3000:], p.stderr[-3000:]))
        return p

    def run_json(self, *args):
        p = self.run_baton(*args)
        try:
            return json.loads(p.stdout)
        except ValueError:
            self.fail('baton %s did not print JSON\n--- stdout ---\n%s\n--- stderr ---\n%s'
                      % (' '.join(args), p.stdout[-3000:], p.stderr[-3000:]))

    def dump(self):
        data = self.run_json('--dump')
        self.assertIsInstance(data, dict)
        self.assertIsInstance(data.get('sessions'), list)
        self.assertIsInstance(data.get('warnings'), list)
        return data

    def stats(self):
        return self.run_json('--stats')

    def kill_check(self, tool, sid):
        data = self.run_json('--kill-check', tool, sid)
        for k in ('ok', 'pids', 'reason'):
            self.assertIn(k, data)
        return data

    # --- lookup helpers

    def find(self, data, sid, tool=None):
        hits = [s for s in data['sessions']
                if s.get('id') == sid and (tool is None or s.get('tool') == tool)]
        if not hits:
            self.fail('session %s not in --dump (ids: %s)'
                      % (sid, [s.get('id') for s in data['sessions']]))
        self.assertEqual(len(hits), 1, 'session %s appears %d times' % (sid, len(hits)))
        return hits[0]

    def absent(self, data, sid):
        self.assertFalse([s for s in data['sessions'] if s.get('id') == sid],
                         'session %s should not be in --dump' % sid)

    # --- Claude builders

    def claude_session(self, cwd, records, sid=None, project=None, raw_tail=None, mtime=None):
        sid = sid or new_uuid()
        pdir = self.projects / (project or slug_for(cwd))
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / ('%s.jsonl' % sid)
        with open(path, 'w') as fh:
            for r in records:
                fh.write(json.dumps(r) + '\n')
            if raw_tail:
                fh.write(raw_tail)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return sid, path

    def claude_raw(self, cwd, name, text, project=None):
        pdir = self.projects / (project or slug_for(cwd))
        pdir.mkdir(parents=True, exist_ok=True)
        path = pdir / name
        path.write_text(text)
        return path

    def claude_child(self, parent_sid, cwd, aid, agent_type, description,
                     entrypoint='cli', project=None):
        sub = self.projects / (project or slug_for(cwd)) / parent_sid / 'subagents'
        sub.mkdir(parents=True, exist_ok=True)
        recs = [c_user(parent_sid, cwd, 'Task: %s' % description, entrypoint=entrypoint,
                       isSidechain=True, agentId=aid),
                c_assistant(parent_sid, cwd, 'done', entrypoint=entrypoint,
                            isSidechain=True, agentId=aid)]
        with open(sub / ('agent-%s.jsonl' % aid), 'w') as fh:
            for r in recs:
                fh.write(json.dumps(r) + '\n')
        (sub / ('agent-%s.meta.json' % aid)).write_text(json.dumps(
            {'agentType': agent_type, 'description': description,
             'toolUseId': 'toolu_%s' % aid, 'spawnDepth': 1}))
        return sub / ('agent-%s.jsonl' % aid)

    def claude_registry(self, pid, sid, cwd, proc_start=LSTART, status='busy',
                        kind='interactive'):
        d = self.home / '.claude' / 'sessions'
        d.mkdir(parents=True, exist_ok=True)
        (d / ('%d.json' % pid)).write_text(json.dumps(
            {'pid': pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': 1790477799738,
             'procStart': proc_start, 'version': '2.1.283', 'peerProtocol': 1,
             'kind': kind, 'entrypoint': 'cli', 'pidDomain': 'darwin',
             'name': 'some-name', 'nameSource': 'auto', 'status': status,
             'updatedAt': 1790577944912, 'statusUpdatedAt': 1790577944912}))

    def proc(self, pid, argv, cwd, lstart=LSTART, ppid=1, open_files=None):
        p = {'pid': pid, 'ppid': ppid, 'lstart': lstart, 'argv': list(argv), 'cwd': cwd}
        if open_files is not None:
            p['open_files'] = [str(f) for f in open_files]
        self.procs.append(p)

    def codex_lock(self, tid):
        """Path of the per-thread writer lock the real codex binary holds open."""
        return self.codex / 'thread-writer-locks' / ('%s.lock' % tid)

    # --- Codex builders

    def codex_rollout(self, tid, cwd, source='cli', messages=(), parent=None,
                      when='2026-09-26T22-27-32'):
        d = self.codex / 'sessions' / '2026' / '09' / '26'
        d.mkdir(parents=True, exist_ok=True)
        path = d / ('rollout-%s-%s.jsonl' % (when, tid))
        meta = {'session_id': tid, 'id': tid, 'timestamp': '2026-09-27T05:27:32.119Z',
                'cwd': cwd,
                'originator': 'codex_exec' if source == 'exec' else 'codex-tui',
                'cli_version': '0.157.1', 'source': source,
                'thread_source': 'user', 'model_provider': 'openai',
                'base_instructions': {'text': 'You are Codex, a coding agent.'},
                'history_mode': 'legacy', 'context_window': 258400}
        if parent:
            meta['parent_thread_id'] = parent
        lines = [{'timestamp': '2026-09-27T05:27:32.120Z', 'type': 'session_meta',
                  'payload': meta}]
        for role, text in messages:
            kind = 'output_text' if role == 'assistant' else 'input_text'
            lines.append({'timestamp': '2026-09-27T05:27:35.000Z', 'type': 'response_item',
                          'payload': {'type': 'message', 'id': 'msg_%s' % new_uuid(),
                                      'role': role,
                                      'content': [{'type': kind, 'text': text}]}})
        with open(path, 'w') as fh:
            for ln in lines:
                fh.write(json.dumps(ln) + '\n')
        return path

    def codex_thread(self, cwd, source='cli', name=None, preview='what is s3?',
                     created=1790486852, updated=1790486872, archived=0,
                     meta_parent=None, messages=None, tid=None, **cols):
        """Create a rollout file and return (tid, row) for codex_db()."""
        tid = tid or new_uuid()
        src_meta = json.loads(source) if isinstance(source, str) and source.startswith('{') else source
        if messages is None:
            messages = [('user', '<environment_context>\n  <cwd>%s</cwd>\n</environment_context>' % cwd),
                        ('user', preview), ('assistant', 'Here is the answer.')]
        path = self.codex_rollout(tid, cwd, source=src_meta, messages=messages, parent=meta_parent)
        src_col = source if isinstance(source, str) else json.dumps(source)
        row = {'id': tid, 'rollout_path': str(path), 'created_at': created,
               'updated_at': updated, 'source': src_col, 'model_provider': 'openai',
               'cwd': cwd, 'title': preview, 'sandbox_policy': '{"type":"danger-full-access"}',
               'approval_mode': 'never', 'archived': archived,
               'cli_version': '0.157.1', 'first_user_message': preview,
               'preview': preview, 'name': name,
               'thread_source': 'user', 'created_at_ms': created * 1000,
               'updated_at_ms': updated * 1000, 'recency_at': updated,
               'recency_at_ms': updated * 1000, 'originator': 'codex-tui',
               'git_branch': 'main'}
        row.update(cols)
        return tid, row

    def codex_db(self, rows=(), version=5, edges=(), schema=None, mtime=None):
        path = self.codex / ('state_%d.sqlite' % version)
        con = sqlite3.connect(str(path))
        con.executescript(CODEX_AUX_SQL)
        con.executescript((schema or CODEX_THREADS_SQL) + ';\n' + CODEX_EDGES_SQL + ';')
        cols = [r[1] for r in con.execute('PRAGMA table_info(threads)')]
        for row in rows:
            use = {k: v for k, v in row.items() if k in cols}
            con.execute('INSERT INTO threads (%s) VALUES (%s)'
                        % (', '.join(use), ', '.join('?' * len(use))), list(use.values()))
        for parent, child, status in edges:
            con.execute('INSERT INTO thread_spawn_edges VALUES (?, ?, ?)',
                        (parent, child, status))
        con.commit()
        con.close()
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def session_index(self, entries):
        with open(self.codex / 'session_index.jsonl', 'a') as fh:
            for tid, name in entries:
                fh.write(json.dumps({'id': tid, 'thread_name': name,
                                     'updated_at': '2026-09-27T05:27:38.998417Z'}) + '\n')


# ======================================================================
# Claude: discovery
# ======================================================================

class ClaudeDiscoveryTests(BatonCase):

    def test_interactive_cli_session_is_root_with_full_record(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        _, path = self.claude_session(cwd, c_basic(sid, cwd, 'plain question', title='Plain title'), sid=sid)
        data = self.dump()
        s = self.find(data, sid, 'claude')
        self.assertEqual(s['kind'], 'root')
        self.assertEqual(s['reason'], '')
        self.assertEqual(s['title'], 'Plain title')
        self.assertEqual(os.path.realpath(s['path']), os.path.realpath(str(path)))
        self.assertEqual(s['cwd'], cwd)
        self.assertEqual(s['branch'], 'main')
        self.assertEqual(s['size'], path.stat().st_size)
        self.assertEqual(s['live'], '')
        self.assertEqual(s['pids'], [])
        self.assertEqual(s['parent'], '')
        self.assertEqual(s['children'], 0)
        for key in ('tool', 'id', 'path', 'cwd', 'kind', 'reason', 'title', 'first_prompt',
                    'last_prompt', 'branch', 'created', 'updated', 'size', 'parent',
                    'agent_type', 'children', 'child_types', 'live', 'pids', 'status'):
            self.assertIn(key, s)

    def test_non_uuid_top_level_files_are_ignored(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'real one'), sid=sid)
        bogus_sid = new_uuid()
        body = ''.join(json.dumps(r) + '\n' for r in c_basic(bogus_sid, cwd, 'not a uuid name'))
        self.claude_raw(cwd, 'notes.jsonl', body)
        self.claude_raw(cwd, 'agent-a1b2c3.jsonl', body)
        self.claude_raw(cwd, '%s.jsonl.bak' % new_uuid(), body)
        data = self.dump()
        claude = [s for s in data['sessions'] if s['tool'] == 'claude']
        self.assertEqual([s['id'] for s in claude], [sid])
        for s in data['sessions']:
            base = os.path.basename(s['path'])
            self.assertNotIn(base, ('notes.jsonl', 'agent-a1b2c3.jsonl'))

    def test_duplicate_uuid_across_project_dirs_keeps_newest(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        now = time.time()
        _, old = self.claude_session(cwd, c_basic(sid, cwd, 'dup prompt', title='Old copy'),
                                     sid=sid, project=slug_for(cwd), mtime=now - 3600)
        _, new = self.claude_session(cwd, c_basic(sid, cwd, 'dup prompt', title='New copy'),
                                     sid=sid, project=slug_for(cwd) + '--claude-worktrees-x',
                                     mtime=now - 60)
        data = self.dump()
        s = self.find(data, sid, 'claude')
        self.assertEqual(os.path.realpath(s['path']), os.path.realpath(str(new)))
        self.assertEqual(s['title'], 'New copy')


# ======================================================================
# Claude: classification
# ======================================================================

class ClaudeClassificationTests(BatonCase):

    def test_sdk_entrypoints_are_automated(self):
        cwd = self.work('alpha')
        ids = {}
        for ep in ('sdk-py', 'sdk-ts', 'sdk-cli'):
            sid = new_uuid()
            self.claude_session(cwd, c_basic(sid, cwd, 'You are a security expert reviewing server-side diff.',
                                             entrypoint=ep, promptSource='sdk'), sid=sid)
            ids[ep] = sid
        root = new_uuid()
        self.claude_session(cwd, c_basic(root, cwd, 'interactive'), sid=root)
        data = self.dump()
        for ep, sid in ids.items():
            with self.subTest(entrypoint=ep):
                s = self.find(data, sid, 'claude')
                self.assertEqual(s['kind'], 'automated')
                self.assertEqual(s['reason'], ep)
        self.assertEqual(self.find(data, root)['kind'], 'root')

    def test_entrypoint_found_only_in_tail_of_large_file(self):
        cwd = self.work('alpha')
        out = {}
        for ep in ('sdk-py', 'cli'):
            sid = new_uuid()
            big = c_user(sid, cwd, 'Review this change for security vulnerabilities:\n' + 'x' * 200000,
                         entrypoint=None)
            recs = [big,
                    c_assistant(sid, cwd, 'no findings', entrypoint=ep),
                    c_last_prompt(sid, 'Review this change for security vulnerabilities')]
            _, path = self.claude_session(cwd, recs, sid=sid)
            self.assertGreater(path.stat().st_size, 128 * 1024)
            out[ep] = sid
        data = self.dump()
        s = self.find(data, out['sdk-py'])
        self.assertEqual((s['kind'], s['reason']), ('automated', 'sdk-py'))
        self.assertEqual(self.find(data, out['cli'])['kind'], 'root')

    def test_sidechain_and_team_sessions_are_automated(self):
        cwd = self.work('alpha')
        side = new_uuid()
        self.claude_session(cwd, c_basic(side, cwd, 'side work', isSidechain=True), sid=side)
        team = new_uuid()
        self.claude_session(cwd, c_basic(team, cwd, 'team work', teamName='red-team'), sid=team)
        data = self.dump()
        s = self.find(data, side)
        self.assertEqual((s['kind'], s['reason']), ('automated', 'sidechain'))
        s = self.find(data, team)
        self.assertEqual((s['kind'], s['reason']), ('automated', 'team'))

    def test_session_kind_daemon_is_automated_bg_is_root(self):
        cwd = self.work('alpha')
        ids = {}
        for kind in ('daemon', 'daemon-worker', 'bg'):
            sid = new_uuid()
            self.claude_session(cwd, c_basic(sid, cwd, 'kind %s' % kind, sessionKind=kind), sid=sid)
            ids[kind] = sid
        data = self.dump()
        for kind in ('daemon', 'daemon-worker'):
            with self.subTest(sessionKind=kind):
                s = self.find(data, ids[kind])
                self.assertEqual((s['kind'], s['reason']), ('automated', 'daemon'))
        self.assertEqual(self.find(data, ids['bg'])['kind'], 'root')

    def test_loop_first_prompt_is_automated(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        loop = ('<command-name>/loop</command-name>\n'
                '            <command-message>loop</command-message>\n'
                '            <command-args>5m /check-deploy</command-args>')
        self.claude_session(cwd, [c_user(sid, cwd, loop),
                                  c_assistant(sid, cwd, 'looping'),
                                  c_last_prompt(sid, '/loop 5m /check-deploy')], sid=sid)
        s = self.find(self.dump(), sid)
        self.assertEqual((s['kind'], s['reason']), ('automated', 'loop'))

    def test_continued_in_supersedes_when_successor_has_messages(self):
        cwd = self.work('webapp')
        a, b = new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(a, cwd, 'old work') + [c_continued_in(a, b)], sid=a)
        self.claude_session(cwd, c_basic(b, cwd, 'continued work', title='webapp-fix-plan'), sid=b)
        data = self.dump()
        s = self.find(data, a)
        self.assertEqual((s['kind'], s['reason']), ('superseded', 'continued-in:%s' % b))
        self.assertEqual(self.find(data, b)['kind'], 'root')

    def test_continued_in_resolved_in_own_project_despite_newer_duplicate(self):
        cwd = self.work('webapp')
        a, b = new_uuid(), new_uuid()
        now = time.time()
        self.claude_session(cwd, c_basic(a, cwd, 'old') + [c_continued_in(a, b)], sid=a, mtime=now - 900)
        self.claude_session(cwd, c_basic(b, cwd, 'new'), sid=b, mtime=now - 600)
        # a newer copy of B in another project folder wins the global dedupe
        self.claude_session(cwd, c_basic(b, cwd, 'new'), sid=b, project=slug_for(cwd) + '-copy',
                            mtime=now - 60)
        self.assertEqual(self.find(self.dump(), a)['kind'], 'superseded')

    def test_continued_in_missing_successor_stays_root(self):
        cwd = self.work('webapp')
        a = new_uuid()
        self.claude_session(cwd, c_basic(a, cwd, 'old work') + [c_continued_in(a, new_uuid())], sid=a)
        s = self.find(self.dump(), a)
        self.assertEqual((s['kind'], s['reason']), ('root', ''))

    def test_continued_in_successor_without_messages_stays_root(self):
        cwd = self.work('webapp')
        a, b = new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(a, cwd, 'old work') + [c_continued_in(a, b)], sid=a)
        # successor exists but holds only bookkeeping records (no "parentUuid")
        self.claude_session(cwd, [{'type': 'mode', 'mode': 'normal', 'sessionId': b},
                                  c_last_prompt(b, '')], sid=b)
        self.assertEqual(self.find(self.dump(), a)['kind'], 'root')

    def test_continued_in_text_inside_a_prompt_is_not_a_record(self):
        cwd = self.work('webapp')
        b, r = new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(b, cwd, 'successor'), sid=b)
        prompt = ('why does this record hide my session? '
                  '{"type":"continued-in","sessionId":"%s","continuedInSessionId":"%s"}' % (r, b))
        self.claude_session(cwd, c_basic(r, cwd, prompt), sid=r)
        s = self.find(self.dump(), r)
        self.assertEqual((s['kind'], s['reason']), ('root', ''))

    def test_small_cli_file_without_user_messages_is_empty(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        recs = [{'type': 'mode', 'mode': 'normal', 'sessionId': sid},
                {'type': 'permission-mode', 'permissionMode': 'default', 'sessionId': sid},
                c_system(sid, cwd),
                c_attachment(sid, cwd, 500),
                c_last_prompt(sid, '')]
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.assertLess(path.stat().st_size, 128 * 1024)
        s = self.find(self.dump(), sid)
        self.assertEqual((s['kind'], s['reason']), ('empty', 'no-user-messages'))

    def test_large_file_with_user_message_only_mid_file_stays_root(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        recs = [c_system(sid, cwd)]
        recs += [c_attachment(sid, cwd, 50000) for _ in range(3)]
        recs.append(c_user(sid, cwd, 'the only question, buried mid-file'))
        recs += [c_attachment(sid, cwd, 50000) for _ in range(3)]
        recs.append(c_assistant(sid, cwd, 'answer'))
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.assertGreater(path.stat().st_size, 256 * 1024)
        s = self.find(self.dump(), sid)
        self.assertEqual(s['kind'], 'root', 'indeterminate must not be classified empty')


# ======================================================================
# Claude: titles
# ======================================================================

class ClaudeTitleTests(BatonCase):

    def test_title_precedence_chain(self):
        cwd = self.work('alpha')
        cases = {}

        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p') + [
            c_ai_title(sid, 'AI One'), c_custom_title(sid, 'Custom Name'),
            c_ai_title(sid, 'AI Two')], sid=sid)
        cases['custom-title record beats ai-title'] = (sid, 'Custom Name')

        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p') + [
            c_custom_title(sid, 'Record Custom'), c_ai_title(sid, 'AI Title')], sid=sid)
        side = self.projects / slug_for(cwd) / sid
        side.mkdir(parents=True, exist_ok=True)
        (side / 'custom-title.json').write_text(json.dumps({'customTitle': 'Sidecar Name'}))
        cases['sidecar custom-title.json beats everything'] = (sid, 'Sidecar Name')

        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p') + [
            c_ai_title(sid, 'First AI'), c_assistant(sid, cwd, 'more'),
            c_ai_title(sid, 'Second AI')], sid=sid)
        cases['last ai-title wins'] = (sid, 'Second AI')

        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p') + [
            c_agent_name(sid, 'agent-handle'), c_ai_title(sid, 'AI Beats Agent Name')], sid=sid)
        cases['agent-name is not part of the chain'] = (sid, 'AI Beats Agent Name')

        sid = new_uuid()
        self.claude_session(cwd, [c_user(sid, cwd, 'first ask'), c_assistant(sid, cwd, 'ok'),
                                  c_last_prompt(sid, 'first ask'),
                                  c_summary('Summary Title')], sid=sid)
        cases['summary beats last-prompt'] = (sid, 'Summary Title')

        sid = new_uuid()
        self.claude_session(cwd, [c_user(sid, cwd, 'first ask'), c_assistant(sid, cwd, 'ok'),
                                  c_user(sid, cwd, 'second ask'), c_assistant(sid, cwd, 'ok'),
                                  c_last_prompt(sid, 'second ask')], sid=sid)
        cases['last-prompt beats first prompt'] = (sid, 'second ask')

        data = self.dump()
        for label, (sid, want) in cases.items():
            with self.subTest(label):
                self.assertEqual(self.find(data, sid)['title'], want)

    def test_first_prompt_fallback_rules(self):
        """No title records and no last-prompt: the first REAL prompt wins."""
        cwd = self.work('beta')
        cases = {}

        def add(label, records_fn, want, exact=True):
            sid = new_uuid()
            self.claude_session(cwd, records_fn(sid), sid=sid)
            cases[label] = (sid, want, exact)

        add('compact summary skipped',
            lambda s: [c_user(s, cwd, 'This session is being continued from a previous conversation '
                                      'that ran out of context. Summary: ...',
                              isCompactSummary=True, isVisibleInTranscriptOnly=True),
                       c_user(s, cwd, 'real question'), c_assistant(s, cwd, 'a')],
            'real question')
        add('tool_result skipped',
            lambda s: [c_tool_result(s, cwd), c_user(s, cwd, 'after tool'), c_assistant(s, cwd, 'a')],
            'after tool')
        add('isMeta skipped',
            lambda s: [c_user(s, cwd, 'Caveat: The messages below were generated by the user '
                                      'while running local commands.', isMeta=True),
                       c_user(s, cwd, 'meta skipped ask'), c_assistant(s, cwd, 'a')],
            'meta skipped ask')
        add('leading system-reminder tag skipped',
            lambda s: [c_user(s, cwd, '<system-reminder>be nice</system-reminder>'),
                       c_user(s, cwd, 'reminder skipped ask'), c_assistant(s, cwd, 'a')],
            'reminder skipped ask')
        add('local-command-caveat skipped',
            lambda s: [c_user(s, cwd, '<local-command-caveat>Caveat: generated</local-command-caveat>'),
                       c_user(s, cwd, 'caveat skipped ask'), c_assistant(s, cwd, 'a')],
            'caveat skipped ask')
        add('request interrupted skipped',
            lambda s: [c_user(s, cwd, '[Request interrupted by user for tool use]'),
                       c_user(s, cwd, 'interrupted skipped ask'), c_assistant(s, cwd, 'a')],
            'interrupted skipped ask')
        add('command with args renders as /cmd args',
            lambda s: [c_user(s, cwd, '<command-name>/deploy-thing</command-name>\n'
                                      '            <command-message>deploy-thing</command-message>\n'
                                      '            <command-args>prod now</command-args>'),
                       c_assistant(s, cwd, 'a')],
            '/deploy-thing prod now')
        add('builtin command skipped for a later real prompt',
            lambda s: [c_user(s, cwd, BUILTIN_MODEL_CMD),
                       c_user(s, cwd, '<local-command-stdout>Set model to Opus</local-command-stdout>'),
                       c_user(s, cwd, 'after builtin ask'), c_assistant(s, cwd, 'a')],
            'after builtin ask')
        add('builtin command is the last-resort fallback',
            lambda s: [c_user(s, cwd, BUILTIN_MODEL_CMD),
                       c_user(s, cwd, '<local-command-stdout>Set model to Opus</local-command-stdout>')],
            'model', False)
        add('bash-input renders as ! cmd',
            lambda s: [c_user(s, cwd, '<bash-input>ls -la</bash-input>'), c_assistant(s, cwd, 'a')],
            '! ls -la')
        add('word "instructions" early is kept',
            lambda s: [c_user(s, cwd, 'Follow these instructions to set up the repo'),
                       c_assistant(s, cwd, 'a')],
            'Follow these instructions to set up the repo')
        add('list content text part',
            lambda s: [c_user(s, cwd, [{'type': 'image', 'source': {'type': 'base64', 'data': 'AAAA'}},
                                       {'type': 'text', 'text': 'list form ask'}]),
                       c_assistant(s, cwd, 'a')],
            'list form ask')
        add('nothing usable falls back to (session)',
            lambda s: [c_user(s, cwd, '<local-command-caveat>Caveat: generated</local-command-caveat>'),
                       c_assistant(s, cwd, 'a')],
            '(session)')

        data = self.dump()
        for label, (sid, want, exact) in cases.items():
            with self.subTest(label):
                got = self.find(data, sid)['title']
                if exact:
                    self.assertEqual(got, want)
                else:
                    self.assertIn(want, got)

    def test_first_prompt_and_last_prompt_fields(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, [
            c_user(sid, cwd, BUILTIN_MODEL_CMD),
            c_user(sid, cwd, 'first thing'), c_assistant(sid, cwd, 'ok'),
            c_user(sid, cwd, 'second thing'), c_assistant(sid, cwd, 'ok'),
            c_last_prompt(sid, 'second thing'), c_ai_title(sid, 'The Title')], sid=sid)
        s = self.find(self.dump(), sid)
        self.assertEqual(s['title'], 'The Title')
        self.assertEqual(s['first_prompt'], 'first thing')
        self.assertEqual(s['last_prompt'], 'second thing')

    def test_torn_tail_keeps_last_complete_title(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p') + [c_ai_title(sid, 'Good Title')], sid=sid,
                            raw_tail='{"type":"ai-title","aiTitle":"Brok')
        self.assertEqual(self.find(self.dump(), sid)['title'], 'Good Title')

    def test_titles_are_single_line(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, [c_user(sid, cwd, 'line one\nline two\n\n\tline three'),
                                  c_assistant(sid, cwd, 'a')], sid=sid)
        s = self.find(self.dump(), sid)
        self.assertNotIn('\n', s['title'])
        self.assertNotIn('\t', s['title'])
        self.assertEqual(norm_ws(s['title']), 'line one line two line three')


# ======================================================================
# Claude: children (Task-tool subagents)
# ======================================================================

class ClaudeChildrenTests(BatonCase):

    def test_subagents_become_children_with_counts(self):
        cwd = self.work('webapp')
        parent = new_uuid()
        self.claude_session(cwd, c_basic(parent, cwd, 'main work', title='Main Session'), sid=parent)
        self.claude_child(parent, cwd, 'a2982fb74b6fa63b4', 'Explore', 'Get submodule diff')
        self.claude_child(parent, cwd, 'a1111111111111111', 'Explore', 'Find callers')
        self.claude_child(parent, cwd, 'a2222222222222222', 'Plan', 'Design the fix')
        data = self.dump()
        p = self.find(data, parent)
        self.assertEqual(p['kind'], 'root')
        self.assertEqual(p['children'], 3)
        self.assertEqual(p['child_types'], {'Explore': 2, 'Plan': 1})
        c = self.find(data, 'a2982fb74b6fa63b4', 'claude')
        self.assertEqual(c['kind'], 'child')
        self.assertEqual(c['reason'], 'subagent')
        self.assertEqual(c['parent'], parent)
        self.assertEqual(c['agent_type'], 'Explore')
        self.assertEqual(c['title'], 'Explore · Get submodule diff')
        self.assertTrue(c['path'].endswith('agent-a2982fb74b6fa63b4.jsonl'))
        # subagent files are never top-level sessions of their own
        self.assertEqual(len([s for s in data['sessions'] if s['tool'] == 'claude']), 4)

    def test_child_precedence_over_automated(self):
        cwd = self.work('webapp')
        parent = new_uuid()
        self.claude_session(cwd, c_basic(parent, cwd, 'main work'), sid=parent)
        self.claude_child(parent, cwd, 'a3333333333333333', 'Explore', 'sdk child', entrypoint='sdk-py')
        c = self.find(self.dump(), 'a3333333333333333', 'claude')
        self.assertEqual((c['kind'], c['reason']), ('child', 'subagent'))


# ======================================================================
# Codex: state DB
# ======================================================================

class CodexDbTests(BatonCase):

    def test_db_kinds_and_titles(self):
        cwd = self.work('astra')
        rows = []
        named, r = self.codex_thread(cwd, name='Explain Amazon S3', preview='what is s3?'); rows.append(r)
        unnamed, r = self.codex_thread(cwd, name=None, preview='in this mac laptop - figure out which model'); rows.append(r)
        exe, r = self.codex_thread(cwd, source='exec', preview='Reply with the single word ok.',
                                   originator='codex_exec'); rows.append(r)
        mcp, r = self.codex_thread(cwd, source='mcp', preview='mcp driven'); rows.append(r)
        vsc, r = self.codex_thread(cwd, source='vscode', name='From VS Code', preview='vs'); rows.append(r)
        arch, r = self.codex_thread(cwd, archived=1, name='Archived one', preview='old',
                                    archived_at=1790486900); rows.append(r)
        unk_json, r = self.codex_thread(cwd, source='{"brandnew":{"x":1}}', preview='future source'); rows.append(r)
        unk_str, r = self.codex_thread(cwd, source='cloud_task', preview='future string source'); rows.append(r)
        self.codex_db(rows)
        data = self.dump()
        s = self.find(data, named, 'codex')
        self.assertEqual((s['kind'], s['reason'], s['title']), ('root', '', 'Explain Amazon S3'))
        self.assertEqual(s['cwd'], cwd)
        s = self.find(data, unnamed, 'codex')
        self.assertEqual((s['kind'], s['title']), ('root', 'in this mac laptop - figure out which model'))
        s = self.find(data, exe, 'codex')
        self.assertEqual((s['kind'], s['reason']), ('automated', 'exec'))
        s = self.find(data, mcp, 'codex')
        self.assertEqual((s['kind'], s['reason']), ('automated', 'mcp'))
        s = self.find(data, vsc, 'codex')
        self.assertEqual((s['kind'], s['title']), ('root', 'From VS Code'))
        self.absent(data, arch)
        for tid in (unk_json, unk_str):
            with self.subTest(source=tid):
                s = self.find(data, tid, 'codex')
                self.assertEqual((s['kind'], s['reason']), ('automated', 'unknown-source'))
        s = self.find(data, named, 'codex')
        self.assertTrue(s['path'].endswith('%s.jsonl' % named))

    def test_db_children_parent_resolution(self):
        cwd = self.work('astra')
        rows = []
        parent, r = self.codex_thread(cwd, name='Parent Thread', preview='orchestrate'); rows.append(r)
        spawn, r = self.codex_thread(
            cwd, source={'subagent': {'thread_spawn': {'parent_thread_id': None, 'depth': 1,
                                                       'agent_path': None, 'agent_nickname': 'Galileo',
                                                       'agent_role': 'worker'}}},
            preview='investigate module A', agent_nickname='Galileo', agent_role='worker',
            thread_source='subagent', tid=None)
        # patch the parent id into the structured source (needs parent's id first)
        src = json.loads(r['source'])
        src['subagent']['thread_spawn']['parent_thread_id'] = parent
        r['source'] = json.dumps(src)
        rows.append(r)
        edge_only, r = self.codex_thread(
            cwd, source={'subagent': {'thread_spawn': {'depth': 1}}},
            preview='investigate module B', thread_source='subagent'); rows.append(r)
        guardian, r = self.codex_thread(
            cwd, source={'subagent': {'other': 'guardian'}}, meta_parent=parent,
            name='Guardian review', preview='Approval review', thread_source='guardian_review')
        rows.append(r)
        self.codex_db(rows, edges=[(parent, spawn, 'closed'), (parent, edge_only, 'open')])
        data = self.dump()
        p = self.find(data, parent, 'codex')
        self.assertEqual(p['kind'], 'root')
        self.assertEqual(p['children'], 3)
        self.assertEqual(sum(p['child_types'].values()), 3)
        s = self.find(data, spawn, 'codex')
        self.assertEqual((s['kind'], s['reason'], s['parent']), ('child', 'thread_spawn', parent))
        s = self.find(data, edge_only, 'codex')
        self.assertEqual((s['kind'], s['reason'], s['parent']), ('child', 'thread_spawn', parent))
        s = self.find(data, guardian, 'codex')
        self.assertEqual((s['kind'], s['reason'], s['parent'], s['agent_type']),
                         ('child', 'guardian', parent, 'guardian'))

    def test_highest_state_db_is_chosen_numerically(self):
        for low, high in ((5, 10), (9, 10)):
            with self.subTest(low=low, high=high):
                for f in self.codex.glob('state_*.sqlite'):
                    f.unlink()
                cwd = self.work('astra')
                tid = new_uuid()
                _, r_low = self.codex_thread(cwd, tid=tid, name='Old DB Name', preview='q')
                _, r_high = self.codex_thread(cwd, tid=tid, name='New DB Name', preview='q')
                now = time.time()
                self.codex_db([r_high], version=high, mtime=now - 3600)
                self.codex_db([r_low], version=low, mtime=now)   # newer mtime, lower number
                s = self.find(self.dump(), tid, 'codex')
                self.assertEqual(s['title'], 'New DB Name')


# ======================================================================
# Codex: rollout fallback
# ======================================================================

class CodexFallbackTests(BatonCase):

    def test_rollout_fallback_skips_developer_and_injected_messages(self):
        cwd = self.work('astra')
        t1 = new_uuid()
        self.codex_rollout(t1, cwd, messages=[
            ('developer', 'You are `/root`, the primary agent in a team of agents.'),
            ('user', '# AGENTS.md instructions for %s\n\n<INSTRUCTIONS>\nbe safe\n</INSTRUCTIONS>' % cwd),
            ('user', '<environment_context>\n  <cwd>%s</cwd>\n</environment_context>' % cwd),
            ('user', 'what is s3?'), ('assistant', 'S3 is object storage.')])
        t2 = new_uuid()
        self.codex_rollout(t2, cwd, messages=[
            ('user', '<user_instructions>\nbe terse\n</user_instructions>'),
            ('user', '<permissions instructions>\nnever\n</permissions instructions>'),
            ('user', '<skills_instructions>\n## Skills\n</skills_instructions>'),
            ('user', '<turn_aborted>\n</turn_aborted>'),
            ('user', '<user_shell_command>\nls\n</user_shell_command>'),
            ('user', '<user_action>\nclicked\n</user_action>'),
            ('user', 'real ask here'), ('assistant', 'ok')])
        t3 = new_uuid()
        self.codex_rollout(t3, cwd, source='exec', messages=[('user', 'Reply with exactly: OK')])
        data = self.dump()
        s = self.find(data, t1, 'codex')
        self.assertEqual((s['kind'], s['title']), ('root', 'what is s3?'))
        s = self.find(data, t2, 'codex')
        self.assertEqual((s['kind'], s['title']), ('root', 'real ask here'))
        self.assertNotEqual(self.find(data, t3, 'codex')['kind'], 'root')

    def test_session_index_last_entry_wins(self):
        cwd = self.work('astra')
        tid = new_uuid()
        self.codex_rollout(tid, cwd, messages=[('user', 'read this file and be prepared to take over'),
                                               ('assistant', 'ok')])
        self.session_index([(tid, 'read this file and be prepared to ta'),
                            (tid, 'Review project for takeover')])
        s = self.find(self.dump(), tid, 'codex')
        self.assertEqual((s['kind'], s['title']), ('root', 'Review project for takeover'))

    def test_unsupported_schema_warns_and_falls_back(self):
        schemas = {
            'missing source': 'CREATE TABLE threads (id TEXT PRIMARY KEY, rollout_path TEXT NOT NULL, '
                              'title TEXT NOT NULL, preview TEXT NOT NULL DEFAULT \'\')',
            'missing preview': CODEX_THREADS_SQL.replace(", preview TEXT NOT NULL DEFAULT ''", ''),
        }
        self.assertNotEqual(schemas['missing preview'], CODEX_THREADS_SQL)
        for label, schema in schemas.items():
            with self.subTest(label):
                for f in self.codex.glob('state_*.sqlite'):
                    f.unlink()
                for f in (self.codex / 'sessions').rglob('*.jsonl'):
                    f.unlink()
                cwd = self.work('astra')
                cli, row = self.codex_thread(cwd, preview='what is s3?', name='DB name ignored')
                exe, row2 = self.codex_thread(cwd, source='exec', preview='Reply with exactly: OK')
                self.codex_db([row, row2], schema=schema)
                data = self.dump()
                self.assertTrue(any('codex state DB unsupported' in w for w in data['warnings']),
                                data['warnings'])
                s = self.find(data, cli, 'codex')
                self.assertEqual((s['kind'], s['title']), ('root', 'what is s3?'))
                self.assertNotEqual(self.find(data, exe, 'codex')['kind'], 'root')


# ======================================================================
# Sanitization
# ======================================================================

class SanitizationTests(BatonCase):

    def assert_clean(self, s):
        for field in ('title', 'first_prompt', 'last_prompt'):
            self.assertIsNone(CONTROL_RE.search(s[field] or ''),
                              '%s has control chars: %r' % (field, s[field]))

    def test_escape_sequences_stripped_claude(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, [
            c_user(sid, cwd, '\x1b[2Jclear\x1b[0m me \x9b31m please\x07'),
            c_assistant(sid, cwd, 'a'),
            c_last_prompt(sid, 'last \x1b]8;;http://x\x1b\\link\x1b]8;;\x1b\\ here'),
            c_ai_title(sid, 'Safe\x1b]0;evil\x07 title\x1b[31m red\x1b[0m')], sid=sid)
        s = self.find(self.dump(), sid)
        self.assert_clean(s)
        self.assertEqual(norm_ws(s['title']), 'Safe title red')
        self.assertNotIn('evil', s['title'])
        self.assertIn('clear', s['first_prompt'])

    def test_escape_sequences_stripped_codex(self):
        cwd = self.work('astra')
        tid, row = self.codex_thread(cwd, name='\x1b[31mRed\x1b[0m Name\x07',
                                     preview='\x1b]0;pwned\x07what is s3?')
        t2, row2 = self.codex_thread(cwd, name=None, preview='\x1b]0;pwned\x07what is s3?')
        self.codex_db([row, row2])
        data = self.dump()
        s = self.find(data, tid, 'codex')
        self.assert_clean(s)
        self.assertEqual(norm_ws(s['title']), 'Red Name')
        s = self.find(data, t2, 'codex')
        self.assert_clean(s)
        self.assertEqual(norm_ws(s['title']), 'what is s3?')


# ======================================================================
# --stats
# ======================================================================

class StatsTests(BatonCase):

    def test_stats_counts_by_kind(self):
        cwd = self.work('webapp')
        a, b, r1, sdk, empty = (new_uuid() for _ in range(5))
        self.claude_session(cwd, c_basic(a, cwd, 'old') + [c_continued_in(a, b)], sid=a)
        self.claude_session(cwd, c_basic(b, cwd, 'new'), sid=b)
        self.claude_session(cwd, c_basic(r1, cwd, 'other root'), sid=r1)
        self.claude_child(r1, cwd, 'a4444444444444444', 'Explore', 'look around')
        self.claude_session(cwd, c_basic(sdk, cwd, 'security review', entrypoint='sdk-py'), sid=sdk)
        self.claude_session(cwd, [c_system(empty, cwd), c_last_prompt(empty, '')], sid=empty)
        rows = []
        c1, r = self.codex_thread(cwd, name='Codex Root'); rows.append(r)
        _, r = self.codex_thread(cwd, source='exec', preview='ok?'); rows.append(r)
        _, r = self.codex_thread(cwd, source={'subagent': {'thread_spawn': {'parent_thread_id': c1, 'depth': 1}}},
                                 preview='child task'); rows.append(r)
        self.codex_db(rows)
        st = self.stats()
        self.assertEqual(st['claude'], {'root': 2, 'automated': 1, 'child': 1, 'superseded': 1, 'empty': 1})
        self.assertEqual(st['codex'], {'root': 1, 'automated': 1, 'child': 1, 'superseded': 0, 'empty': 0})
        self.assertIsInstance(st['warnings'], list)

    def test_stats_all_keys_present_when_empty(self):
        st = self.stats()
        for tool in ('claude', 'codex'):
            self.assertEqual(st[tool], {k: 0 for k in KINDS})


# ======================================================================
# Persistent cache
# ======================================================================

class CacheTests(BatonCase):

    def cache_file(self):
        return self.home / '.cache' / 'baton' / 'index.json'

    def populate(self, n=6):
        cwd = self.work('alpha')
        ids = []
        for i in range(n):
            sid = new_uuid()
            self.claude_session(cwd, c_basic(sid, cwd, 'prompt %d' % i, title='Title %d' % i), sid=sid)
            ids.append(sid)
        rows = []
        for i in range(3):
            _, r = self.codex_thread(cwd, name='Codex %d' % i); rows.append(r)
        self.codex_db(rows)
        return cwd, ids

    @staticmethod
    def canon(data):
        return sorted(data['sessions'], key=lambda s: (s['tool'], s['id']))

    def test_cache_permissions_and_warm_run_is_identical(self):
        self.populate()
        first = self.dump()
        cf = self.cache_file()
        self.assertTrue(cf.is_file(), 'cache not written at %s' % cf)
        self.assertEqual(stat.S_IMODE(cf.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(cf.parent.stat().st_mode), 0o700)
        json.loads(cf.read_text())
        second = self.dump()
        self.assertEqual(self.canon(first), self.canon(second))
        self.assertEqual(first['warnings'], second['warnings'])

    def test_cache_invalidated_when_file_changes(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        _, path = self.claude_session(cwd, c_basic(sid, cwd, 'p', title='Old Title'), sid=sid)
        self.assertEqual(self.find(self.dump(), sid)['title'], 'Old Title')
        with open(path, 'a') as fh:
            fh.write(json.dumps(c_ai_title(sid, 'New Title')) + '\n')
        st = path.stat()
        os.utime(path, (st.st_atime, st.st_mtime + 5))
        self.assertEqual(self.find(self.dump(), sid)['title'], 'New Title')

    def test_sidecar_and_codex_rename_seen_without_transcript_change(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p', title='AI Name'), sid=sid)
        tid, row = self.codex_thread(cwd, name='Before Rename')
        db = self.codex_db([row])
        data = self.dump()
        self.assertEqual(self.find(data, sid)['title'], 'AI Name')
        self.assertEqual(self.find(data, tid)['title'], 'Before Rename')
        side = self.projects / slug_for(cwd) / sid
        side.mkdir(parents=True, exist_ok=True)
        (side / 'custom-title.json').write_text(json.dumps({'customTitle': 'Renamed Via Sidecar'}))
        con = sqlite3.connect(str(db))
        con.execute('UPDATE threads SET name = ? WHERE id = ?', ('After Rename', tid))
        con.commit()
        con.close()
        data = self.dump()
        self.assertEqual(self.find(data, sid)['title'], 'Renamed Via Sidecar')
        self.assertEqual(self.find(data, tid)['title'], 'After Rename')

    def test_new_child_and_successor_seen_on_warm_run(self):
        cwd = self.work('alpha')
        a, b = new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(a, cwd, 'old') + [c_continued_in(a, b)], sid=a)
        data = self.dump()
        self.assertEqual(self.find(data, a)['kind'], 'root')
        self.assertEqual(self.find(data, a)['children'], 0)
        # successor appears and a child is spawned; A's own file is untouched
        self.claude_session(cwd, c_basic(b, cwd, 'new'), sid=b)
        self.claude_child(a, cwd, 'a5555555555555555', 'Plan', 'late child')
        data = self.dump()
        self.assertEqual(self.find(data, a)['kind'], 'superseded')
        self.assertEqual(self.find(data, a)['children'], 1)

    def test_torn_tail_does_not_replace_good_cached_title(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        _, path = self.claude_session(cwd, c_basic(sid, cwd, 'p', title='Good'), sid=sid)
        self.assertEqual(self.find(self.dump(), sid)['title'], 'Good')
        with open(path, 'a') as fh:
            fh.write('{"type":"ai-title","aiTitle":"Half')
        self.assertEqual(self.find(self.dump(), sid)['title'], 'Good')
        with open(path, 'a') as fh:
            fh.write('Done","sessionId":"%s"}\n' % sid)
        self.assertEqual(self.find(self.dump(), sid)['title'], 'HalfDone')

    def test_concurrent_writers(self):
        self.populate(n=30)
        env = self.env()
        procs = [subprocess.Popen([str(BATON), '--dump'], env=env, cwd=str(self.home),
                                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE, text=True, start_new_session=True)
                 for _ in range(4)]
        results = []
        for p in procs:
            try:
                out, err = p.communicate(timeout=TIMEOUT)
            except subprocess.TimeoutExpired:
                p.kill()
                self.fail('concurrent baton --dump timed out')
            self.assertEqual(p.returncode, 0, err[-2000:])
            results.append(json.loads(out))
        sets = [sorted((s['tool'], s['id'], s['kind'], s['title']) for s in r['sessions'])
                for r in results]
        for other in sets[1:]:
            self.assertEqual(sets[0], other)
        self.assertEqual(len(sets[0]), 33)
        json.loads(self.cache_file().read_text())
        # and a following warm run still agrees
        warm = sorted((s['tool'], s['id'], s['kind'], s['title']) for s in self.dump()['sessions'])
        self.assertEqual(warm, sets[0])


# ======================================================================
# Live detection + kill check
# ======================================================================

class LiveTests(BatonCase):

    def two_claude(self):
        cwd = self.work('webapp')
        live, other = new_uuid(), new_uuid()
        now = time.time()
        self.claude_session(cwd, c_basic(live, cwd, 'live one', title='webapp-fix-plan'),
                            sid=live, mtime=now - 30)
        self.claude_session(cwd, c_basic(other, cwd, 'older one'), sid=other, mtime=now - 7200)
        return cwd, live, other

    def test_claude_registry_exact_live(self):
        cwd, live, other = self.two_claude()
        self.claude_registry(4100, live, cwd, status='busy')
        # whitespace differs from procStart on purpose (ps pads the day)
        self.proc(4100, [CLAUDE_BIN, '--settings', '{"modelPicker":{}}'], cwd,
                  lstart='Sun Sep 27  02:56:37 2026')
        data = self.dump()
        s = self.find(data, live)
        self.assertEqual((s['live'], s['pids'], s['status']), ('exact', [4100], 'busy'))
        s = self.find(data, other)
        self.assertEqual((s['live'], s['pids']), ('', []))
        kc = self.kill_check('claude', live)
        self.assertTrue(kc['ok'], kc)
        self.assertEqual(kc['pids'], [4100])
        self.assertFalse(self.kill_check('claude', other)['ok'])

    def test_claude_idle_and_waiting_status_reported(self):
        for status in ('idle', 'waiting'):
            with self.subTest(status=status):
                self.procs = []
                shutil.rmtree(self.home / '.claude' / 'sessions', ignore_errors=True)
                shutil.rmtree(self.projects, ignore_errors=True)
                self.projects.mkdir(parents=True)
                cwd, live, _ = self.two_claude()
                self.claude_registry(4101, live, cwd, status=status)
                self.proc(4101, [CLAUDE_BIN], cwd)
                s = self.find(self.dump(), live)
                self.assertEqual((s['live'], s['status']), ('exact', status))

    def test_claude_registry_status_is_whitelisted(self):
        cwd, live, _ = self.two_claude()
        self.claude_registry(26931, live, cwd, status='busy\x1b]0;pwned\x07')
        self.proc(26931, [CLAUDE_BIN], cwd)
        s = self.find(self.dump(), live)
        self.assertEqual((s['live'], s['status']), ('exact', ''))
        kc = self.kill_check('claude', live)
        self.assertNotIn('\x1b', json.dumps(kc))

    def test_claude_stale_pid_not_live(self):
        cwd, live, _ = self.two_claude()
        self.claude_registry(4243, live, cwd)
        s = self.find(self.dump(), live)
        self.assertEqual((s['live'], s['pids']), ('', []))
        kc = self.kill_check('claude', live)
        self.assertFalse(kc['ok'])
        self.assertEqual(kc['pids'], [])

    def test_claude_procstart_mismatch_not_live(self):
        cwd, live, _ = self.two_claude()
        self.claude_registry(4244, live, cwd)
        self.proc(4244, [CLAUDE_BIN], cwd, lstart='Mon Sep 28 09:14:15 2026')   # pid reused
        s = self.find(self.dump(), live)
        self.assertEqual(s['live'], '')
        self.assertFalse(self.kill_check('claude', live)['ok'])

    def test_claude_non_claude_executable_not_live(self):
        cwd, live, _ = self.two_claude()
        self.claude_registry(4245, live, cwd)
        self.proc(4245, ['/opt/homebrew/bin/bash', '-c', 'source snapshot.sh && claude-code stuff'], cwd)
        s = self.find(self.dump(), live)
        self.assertEqual(s['live'], '')
        self.assertFalse(self.kill_check('claude', live)['ok'])

    def test_claude_without_registry_is_never_killable(self):
        cwd, live, _ = self.two_claude()
        self.proc(4246, [CLAUDE_BIN, '--settings', '{}'], cwd)   # no ~/.claude/sessions at all
        s = self.find(self.dump(), live)
        self.assertNotEqual(s['live'], 'exact')
        self.assertFalse(self.kill_check('claude', live)['ok'])

    def two_codex(self):
        cwd = self.work('astra')
        newer, r1 = self.codex_thread(cwd, name='Newer', updated=1790590000)
        older, r2 = self.codex_thread(cwd, name='Older', updated=1790500000)
        self.codex_db([r1, r2])
        return cwd, newer, older

    def test_codex_argv_thread_id_is_not_proof(self):
        """ps flattens argv, and `codex <uuid>` is a NEW session whose prompt is
        an id: an argv token never authorizes a kill. Only the lock does."""
        cwd, newer, older = self.two_codex()
        self.proc(5000, [CODEX_BIN, 'resume', older], cwd)
        data = self.dump()
        self.assertEqual(self.find(data, older, 'codex')['live'], '')
        self.assertEqual(self.find(data, newer, 'codex')['live'], 'maybe')
        self.assertFalse(self.kill_check('codex', older)['ok'])
        self.assertFalse(self.kill_check('codex', newer)['ok'])

    def test_codex_bare_uuid_prompt_is_not_proof(self):
        cwd, newer, older = self.two_codex()
        self.proc(5002, [CODEX_BIN, older], cwd)
        self.assertNotEqual(self.find(self.dump(), older, 'codex')['live'], 'exact')
        self.assertFalse(self.kill_check('codex', older)['ok'])

    def test_codex_value_flags_and_non_tui_subcommands(self):
        cwd, newer, older = self.two_codex()
        self.proc(5003, [CODEX_BIN, '--add-dir', '/x', 'exec', 'do it'], cwd)
        for pid, sub in ((5004, 'review'), (5005, 'queue'), (5006, 'archive'), (5007, 'delete')):
            self.proc(pid, [CODEX_BIN, sub, older], cwd)
        data = self.dump()
        for tid in (newer, older):
            self.assertEqual(self.find(data, tid, 'codex')['live'], '')

    def test_codex_exact_via_thread_writer_lock_in_three_level_chain(self):
        """Real codex: shim -> launcher -> real binary, all basename 'codex',
        same cwd, plain argv; only the real binary holds the thread lock."""
        cwd, newer, older = self.two_codex()
        self.proc(8000, ['/usr/local/bin/codex', '--dangerously-bypass-approvals-and-sandbox'], cwd)
        self.proc(8001, ['/opt/codex/0.157.1/bin/codex',
                         '--dangerously-bypass-approvals-and-sandbox'], cwd, ppid=8000)
        self.proc(8002, [CODEX_BIN, '--dangerously-bypass-approvals-and-sandbox'], cwd, ppid=8001,
                  open_files=[self.codex_lock(older), self.codex / 'state_5.sqlite',
                              self.codex / 'state_5.sqlite-wal'])
        data = self.dump()
        s = self.find(data, older, 'codex')
        self.assertEqual((s['live'], s['pids']), ('exact', [8002]))
        # the running chain is accounted for; the other thread must not be flagged
        self.assertEqual(self.find(data, newer, 'codex')['live'], '')
        kc = self.kill_check('codex', older)
        self.assertTrue(kc['ok'], kc)
        self.assertEqual(kc['pids'], [8002])
        self.assertFalse(self.kill_check('codex', newer)['ok'])

    def test_codex_exact_via_open_rollout_file(self):
        cwd, newer, older = self.two_codex()
        rollout = next((self.codex / 'sessions').rglob('rollout-*-%s.jsonl' % newer))
        self.proc(8100, [CODEX_BIN], cwd, open_files=[rollout])
        data = self.dump()
        s = self.find(data, newer, 'codex')
        self.assertEqual((s['live'], s['pids']), ('exact', [8100]))
        self.assertEqual(self.find(data, older, 'codex')['live'], '')
        self.assertTrue(self.kill_check('codex', newer)['ok'])

    def test_codex_lock_held_by_exec_is_that_threads_writer(self):
        """Whoever holds a thread's writer lock IS its writer, exec or not."""
        cwd, newer, older = self.two_codex()
        self.proc(8200, [CODEX_BIN, 'exec', 'do a thing'], cwd, open_files=[self.codex_lock(older)])
        s = self.find(self.dump(), older, 'codex')
        self.assertEqual((s['live'], s['pids']), ('exact', [8200]))
        self.assertTrue(self.kill_check('codex', older)['ok'])

    def test_codex_lock_held_by_a_server_is_ignored(self):
        """app-server / mcp-server can serve several threads: never killable."""
        cwd, newer, older = self.two_codex()
        self.proc(8201, [CODEX_BIN, 'app-server'], cwd, open_files=[self.codex_lock(older)])
        self.assertEqual(self.find(self.dump(), older, 'codex')['live'], '')
        self.assertFalse(self.kill_check('codex', older)['ok'])

    def test_codex_resume_chain_is_exact_through_its_lock(self):
        cwd, newer, older = self.two_codex()
        self.proc(6000, ['/usr/local/bin/codex', 'resume', older], cwd)
        self.proc(6001, [CODEX_BIN, 'resume', older], cwd, ppid=6000,
                  open_files=[self.codex_lock(older)])
        data = self.dump()
        s = self.find(data, older, 'codex')
        self.assertEqual((s['live'], s['pids']), ('exact', [6001]))
        self.assertEqual(self.find(data, newer, 'codex')['live'], '')

    def test_codex_maybe_marks_only_newest_root_in_cwd(self):
        cwd, newer, older = self.two_codex()
        self.proc(5001, ['/usr/local/bin/codex', '--dangerously-bypass-approvals-and-sandbox'], cwd)
        data = self.dump()
        s = self.find(data, newer, 'codex')
        self.assertEqual((s['live'], s['pids']), ('maybe', [5001]))
        self.assertEqual(self.find(data, older, 'codex')['live'], '')
        kc = self.kill_check('codex', newer)
        self.assertFalse(kc['ok'])
        self.assertEqual(kc['reason'], 'maybe')

    def test_codex_internal_and_non_tui_processes_ignored(self):
        cwd, newer, older = self.two_codex()
        self.proc(7000, ['/usr/local/bin/codex', '__otel-server'], cwd)
        self.proc(7001, [CODEX_BIN, 'exec', '--json', 'resume', older], cwd)
        self.proc(7002, [CODEX_BIN, 'app-server'], cwd)
        self.proc(7003, [CODEX_BIN, 'mcp-server'], cwd)
        self.proc(7004, ['/usr/local/bin/codex-helper', 'resume', older], cwd)
        data = self.dump()
        for tid in (newer, older):
            with self.subTest(tid=tid):
                s = self.find(data, tid, 'codex')
                self.assertEqual((s['live'], s['pids']), ('', []))
        self.assertFalse(self.kill_check('codex', older)['ok'])

    def test_kill_check_unknown_session(self):
        kc = self.kill_check('claude', new_uuid())
        self.assertFalse(kc['ok'])
        self.assertEqual(kc['pids'], [])



# ======================================================================
# Render / key dispatcher / preview (the fzf-facing contract)
# ======================================================================

class RenderTests(BatonCase):
    """TSV rows: 1 path · 2 resume tool · 3 resume id · 4 resume cwd · 5 folder ·
    6 unfold key · 7 row type · 8 live · 9 card · 10 display."""

    def render(self, query='', state=None, extra_env=None):
        if state is not None:
            (self.home / 'state').write_text(state)
        env = self.env()
        env['FZF_LINES'] = '60'
        if extra_env:
            env.update(extra_env)
        p = subprocess.run([str(BATON), '--render', query], env=env, cwd=str(self.home),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT, start_new_session=True)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        rows = [ln.split('\t') for ln in p.stdout.splitlines()]
        for r in rows:
            self.assertEqual(len(r), 10, r)
        return rows

    def key(self, *args):
        env = self.env()
        p = subprocess.run([str(BATON), '--key'] + list(args), env=env, cwd=str(self.home),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT, start_new_session=True)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return p.stdout

    def fixture(self):
        cwd = self.work('proj one')                       # space in the folder name
        now = time.time()
        parent, closed, sdk = new_uuid(), new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(parent, cwd, 'build it', title='Parent Session'),
                            sid=parent, mtime=now - 60)
        self.claude_child(parent, cwd, 'a1111111111111111', 'Explore', 'Find callers')
        self.claude_session(cwd, c_basic(closed, cwd, 'older', title='Closed Session'),
                            sid=closed, mtime=now - 7200)
        self.claude_session(cwd, c_basic(sdk, cwd, 'You are a security expert', entrypoint='sdk-py'),
                            sid=sdk, mtime=now - 30)
        return cwd, parent, closed, sdk

    def test_default_view_rows(self):
        cwd, parent, closed, sdk = self.fixture()
        rows = self.render()
        self.assertEqual(rows[0][6], 'status')
        self.assertIn('hidden: 1 automated · 1 subagents', rows[0][9])
        self.assertEqual([r[6] for r in rows[1:]], ['folder', 'session', 'session'])
        folder = rows[1]
        self.assertEqual((folder[2], folder[4]), ('', cwd))          # not selectable
        p = rows[2]
        self.assertEqual((p[1], p[2], p[3], p[5]), ('claude', parent, cwd, 'claude:%s' % parent))
        self.assertIn('Parent Session', p[9])
        self.assertIn('⤷1', p[9])
        self.assertEqual(rows[3][5], '', 'no unfold key without children')
        self.assertNotIn(sdk, [r[2] for r in rows])

    def test_unfold_all_mode_and_expand_via_key_dispatcher(self):
        cwd, parent, closed, sdk = self.fixture()
        (self.home / 'state').write_text('tool=both\nall=0\nexpanded=\nunfolded=\n')
        out = self.key('right', '', 'claude:%s' % parent, 'session')
        self.assertIn('track-current+reload(', out)
        rows = self.render()
        kinds = [r[6] for r in rows]
        self.assertEqual(kinds, ['status', 'folder', 'session', 'child', 'session'])
        child = rows[3]
        # Enter on a child resumes the PARENT
        self.assertEqual((child[1], child[2], child[3]), ('claude', parent, cwd))
        self.assertTrue(child[0].endswith('agent-a1111111111111111.jsonl'))
        # fold from the child row
        self.key('left', '', 'claude:%s' % parent, 'child')
        self.assertEqual([r[6] for r in self.render()], ['status', 'folder', 'session', 'session'])
        # ctrl-a shows automated sessions, tagged
        self.assertIn('change-prompt(both+all> )', self.key('ctrl-a', ''))
        rows = self.render()
        sdk_rows = [r for r in rows if r[2] == sdk]
        self.assertEqual(len(sdk_rows), 1)
        self.assertIn('[sdk-py]', sdk_rows[0][9])
        # + on a folder with a space in its path round-trips through the state
        out = self.key('plus', '', cwd)
        self.assertIn('reload(', out)
        state = (self.home / 'state').read_text()
        self.assertIn('expanded=', state)
        self.assertNotIn(' one', state.split('expanded=')[1].split('\n')[0])   # %-encoded
        self.assertNotIn('press +', ''.join(r[9] for r in self.render()))

    def test_keys_keep_editing_meaning_while_typing(self):
        self.assertEqual(self.key('plus', 'c', '/x'), 'put(+)')
        self.assertEqual(self.key('equal', 'a', '/x'), 'put(=)')
        self.assertEqual(self.key('right', 'q', 'claude:x', 'session'), 'forward-char')
        self.assertEqual(self.key('left', 'q', 'claude:x', 'session'), 'backward-char')
        self.assertEqual(self.key('esc', 'q'), 'clear-query')
        self.assertEqual(self.key('esc', ''), 'abort')
        self.assertEqual(self.key('resize', 'q'), 'ignore')
        # no tracking while typing (it would block query input)
        self.assertNotIn('track-current', self.key('ctrl-t', 'q'))
        self.assertIn('track-current', self.key('ctrl-t', ''))

    def test_search_matches_titles_and_skips_hidden(self):
        cwd, parent, closed, sdk = self.fixture()
        rows = self.render('closed')
        self.assertEqual(rows[0][6], 'status')
        self.assertIn('1 match for "closed"', rows[0][9])
        self.assertEqual([r[2] for r in rows if r[6] == 'session'], [closed])
        rows = self.render('security')            # only in the hidden SDK session
        self.assertEqual([r for r in rows if r[6] == 'session'], [])

    def test_launch_folder_floats_up(self):
        a = self.work('aaa')
        b = self.work('bbb')
        now = time.time()
        s1, s2 = new_uuid(), new_uuid()
        self.claude_session(a, c_basic(s1, a, 'x', title='In A'), sid=s1, mtime=now - 10)
        self.claude_session(b, c_basic(s2, b, 'y', title='In B'), sid=s2, mtime=now - 9000)
        rows = self.render(extra_env={'BATON_ORIGIN': os.path.join(b, 'sub')})
        folders = [r[4] for r in rows if r[6] == 'folder']
        self.assertEqual(folders, [b, a])
        self.assertIn('here', [r for r in rows if r[6] == 'folder'][0][9])

    def test_kill_check_us_format_keeps_empty_fields(self):
        cwd, parent, closed, sdk = self.fixture()
        p = self.run_baton('--kill-check', 'claude', closed, 'us')
        fields = p.stdout.rstrip('\n').split('\x1f')
        self.assertEqual(len(fields), 6, fields)
        self.assertEqual(fields[0], '0')
        self.assertEqual(fields[2], 'Closed Session')

    def test_preview_card_height_and_width(self):
        cwd = self.work('proj')
        sid = new_uuid()
        title = '日本語のタイトル ' * 10
        _, path = self.claude_session(cwd, c_basic(sid, cwd, 'ask', title=title), sid=sid)
        rows = self.render()
        row = [r for r in rows if r[2] == sid][0]
        env = self.env()
        env['FZF_PREVIEW_COLUMNS'] = '30'
        p = subprocess.run([str(BATON), '--preview', row[0], row[1], row[8]], env=env,
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        lines = p.stdout.split('\n')
        strip = lambda s: re.sub(r'\x1b\[[0-9;]*m', '', s)
        import unicodedata
        width = lambda s: sum(2 if unicodedata.east_asian_width(c) in 'WF' else 1 for c in s)
        card = [strip(l) for l in lines[:7]]
        self.assertTrue(card[0].startswith('日本語'))
        self.assertTrue(set(card[6]) == {'─'}, card[6])
        for l in card:
            self.assertLessEqual(width(l), 29, l)
        self.assertIn('You:', p.stdout)
        # folder rows preview a folder card
        folder = [r for r in rows if r[6] == 'folder'][0]
        p = subprocess.run([str(BATON), '--preview', '', '', folder[8]], env=env,
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertIn('1 session', p.stdout)


    def test_preview_reaches_back_past_tool_traffic(self):
        """The last real question can sit megabytes back behind tool output."""
        cwd = self.work('proj')
        sid = new_uuid()
        recs = [c_user(sid, cwd, 'the only real question')]
        for _ in range(30):                              # ~3 MB of tool traffic
            recs.append(c_tool_result(sid, cwd))
            recs[-1]['message']['content'][0]['content'] = 'x' * 100000
        recs.append(c_assistant(sid, cwd, 'final answer'))
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.assertGreater(path.stat().st_size, 2 * 1024 * 1024)
        p = subprocess.run([str(BATON), '--preview', str(path), 'claude', ''], env=self.env(),
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertIn('the only real question', p.stdout)
        self.assertIn('final answer', p.stdout)


class CacheHygieneTests(BatonCase):

    def test_stale_temp_files_are_swept(self):
        cwd = self.work('alpha')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'p', title='T'), sid=sid)
        d = self.home / '.cache' / 'baton'
        d.mkdir(parents=True)
        old = d / 'index.abandoned.tmp'
        fresh = d / 'index.inflight.tmp'
        old.write_text('x')
        fresh.write_text('x')
        past = time.time() - 3600
        os.utime(old, (past, past))
        self.dump()
        self.assertFalse(old.exists(), 'stale temp file should be removed')
        self.assertTrue(fresh.exists(), 'a recent temp file may belong to a live writer')



class KillTests(BatonCase):
    """--kill re-validates and signals in one step, against REAL processes
    (no BATON_TEST_PROCS): a fake `claude` is /bin/sleep with argv[0] set."""

    def real_env(self):
        e = self.env()
        e.pop('BATON_TEST_PROCS')
        return e

    def spawn_claude(self, ignore_term=False):
        import signal
        argv0 = str(self.home / 'bin' / 'claude')
        pre = (lambda: signal.signal(signal.SIGTERM, signal.SIG_IGN)) if ignore_term else None
        p = subprocess.Popen([argv0, '600'], executable='/bin/sleep', preexec_fn=pre,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self._reap, p)
        time.sleep(0.2)
        lst = subprocess.run(['ps', '-o', 'lstart=', '-p', str(p.pid)], capture_output=True, text=True,
                             env=dict(os.environ, TZ='UTC', LC_ALL='C')).stdout.strip()
        return p, lst

    @staticmethod
    def _reap(p):
        try:
            p.kill()
        except OSError:
            pass
        p.wait()

    def run_real(self, *args):
        p = subprocess.run([str(BATON)] + list(args), env=self.real_env(), cwd=str(self.home),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT, start_new_session=True)
        self.assertEqual(p.returncode, 0, p.stderr[-2000:])
        return p.stdout.rstrip('\n').split('\x1f')

    def live_session(self, ignore_term=False):
        cwd = self.work('proj')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'live', title='Live One'), sid=sid)
        p, lst = self.spawn_claude(ignore_term)
        self.claude_registry(p.pid, sid, cwd, proc_start=lst, status='busy')
        return sid, p

    def test_kill_ends_the_proven_process(self):
        sid, p = self.live_session()
        ok, pids = self.run_real('--kill-check', 'claude', sid, 'us')[:2]
        self.assertEqual((ok, pids), ('1', str(p.pid)))
        self.assertEqual(self.run_real('--kill', 'claude', sid, str(p.pid)), ['ended', str(p.pid)])
        p.wait(timeout=5)

    def test_kill_refuses_when_the_confirmed_pids_changed(self):
        sid, p = self.live_session()
        out = self.run_real('--kill', 'claude', sid, '999999')
        self.assertEqual(out[0], 'changed')
        self.assertIsNone(p.poll(), 'a mismatched confirmation must not signal anything')

    def test_kill_reports_a_process_that_ignores_sigterm(self):
        sid, p = self.live_session(ignore_term=True)
        out = self.run_real('--kill', 'claude', sid, str(p.pid))
        self.assertEqual(out, ['still-running', str(p.pid)])
        self.assertIsNone(p.poll())

    def test_kill_never_signals_in_test_mode(self):
        sid, p = self.live_session()
        out = self.run_baton('--kill', 'claude', sid, str(p.pid)).stdout.rstrip('\n').split('\x1f')
        self.assertEqual(out[0], 'refused')
        self.assertIsNone(p.poll())


class PickerSafetyTests(BatonCase):

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf (the picker path runs only with it)')
    def test_mktemp_failure_exits_without_touching_the_cwd(self):
        import pty
        work = Path(self.work('cwd'))
        keep = work / '.precious.tmp'
        keep.write_text('keep me')
        env = self.env()
        stub = self.home / 'stub-bin'
        stub.mkdir()
        (stub / 'mktemp').write_text('#!/bin/sh\nexit 1\n')      # mktemp fails
        (stub / 'mktemp').chmod(0o755)
        env['PATH'] = '%s:%s' % (stub, env['PATH'])
        pid, fd = pty.fork()
        if pid == 0:                                    # child: gets a controlling tty
            os.chdir(str(work))
            os.execve(str(BATON), [str(BATON)], env)
        out = b''
        deadline = time.time() + TIMEOUT
        while time.time() < deadline:
            try:
                chunk = os.read(fd, 4096)
            except OSError:
                break
            if not chunk:
                break
            out += chunk
        _, status = os.waitpid(pid, 0)
        self.assertNotEqual(os.waitstatus_to_exitcode(status), 0, out)
        self.assertIn(b'cannot create a temporary state file', out)
        self.assertTrue(keep.exists(), 'cleanup must never glob relative to the cwd')


class MoreRenderTests(BatonCase):
    render = RenderTests.render

    def test_children_of_a_visible_automated_parent_resume_it(self):
        cwd = self.work('proj')
        sdk = new_uuid()
        self.claude_session(cwd, c_basic(sdk, cwd, 'review', entrypoint='sdk-py'), sid=sdk)
        self.claude_child(sdk, cwd, 'a9999999999999999', 'Explore', 'sdk helper')
        rows = self.render(state='tool=both\nall=1\nexpanded=\nunfolded=claude%%3A%s\n' % sdk)
        child = [r for r in rows if r[6] == 'child']
        self.assertEqual(len(child), 1)
        self.assertEqual((child[0][1], child[0][2]), ('claude', sdk))

    @unittest.skipUnless(shutil.which('rg'), 'content search needs ripgrep')
    def test_search_finds_text_only_in_the_conversation_body(self):
        cwd = self.work('proj')
        sid = new_uuid()
        self.claude_session(cwd, [c_user(sid, cwd, 'start'),
                                  c_assistant(sid, cwd, 'the answer mentions zanzibarquux once'),
                                  c_last_prompt(sid, 'start'), c_ai_title(sid, 'Plain Title')], sid=sid)
        rows = self.render('zanzibarquux')
        hits = [r for r in rows if r[6] == 'session']
        self.assertEqual([r[2] for r in hits], [sid])
        self.assertIn('zanzibarquux', hits[0][9])



def load_scanner(home):
    """The embedded python scanner as a module-like dict, for unit tests of
    internals that are hard to reach through the CLI (races, torn reads)."""
    src = BATON.read_text()
    code = re.search(r"python3 - \"\$@\" <<'PY'\n(.*?)\nPY", src, re.S).group(1)
    tail = "try:\n    sys.exit(main(sys.argv))\nexcept BrokenPipeError:\n    sys.exit(0)"
    assert tail in code
    code = code.replace(tail, '')
    from unittest import mock
    with mock.patch.dict(os.environ, {'HOME': str(home)}):
        g = {'__name__': 'baton_scanner'}
        exec(compile(code, 'baton-scanner', 'exec'), g)
    return g


class ScannerInternalsTests(BatonCase):

    def test_kill_refuses_a_pid_recycled_after_the_proof(self):
        """The proof carries each pid's identity (start time at proof). If the
        pid's current start time differs, it was recycled: never signal it."""
        import signal
        g = load_scanner(self.home)
        p = subprocess.Popen(['/bin/sleep', '600'])
        self.addCleanup(lambda: (p.kill(), p.wait()))
        g['kill_check'] = lambda tool, sid: {'ok': True, 'pids': [p.pid], 'reason': 'exact',
                                             'ident': {p.pid: 'Mon Jan  1 00:00:00 2001'}}
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BATON_TEST_PROCS', None)
            out = g['kill_session']('claude', 'x', str(p.pid))
        self.assertEqual(out['result'], 'changed')
        time.sleep(0.2)
        self.assertIsNone(p.poll(), 'a recycled pid must not be signalled')

    def test_kill_signals_when_identity_matches(self):
        g = load_scanner(self.home)
        p = subprocess.Popen(['/bin/sleep', '600'])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        ls = g['_lstart'](p.pid)
        self.assertTrue(ls)
        g['kill_check'] = lambda tool, sid: {'ok': True, 'pids': [p.pid], 'reason': 'exact',
                                             'ident': {p.pid: ls}}
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BATON_TEST_PROCS', None)
            out = g['kill_session']('claude', 'x', str(p.pid))
        self.assertEqual(out['result'], 'ended')      # zombie counts as gone
        self.assertEqual(p.wait(timeout=5), -15)

    def test_kill_skips_a_pid_recycled_between_preflight_and_signal(self):
        g = load_scanner(self.home)
        p = subprocess.Popen(['/bin/sleep', '600'])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        real = g['_lstart'](p.pid)
        calls = []
        def seq(pid):                                   # preflight sees the original,
            calls.append(pid)                           # every later look sees a stranger
            return real if len(calls) == 1 else 'Mon Jan  1 00:00:00 2001'
        g['_lstart'] = seq
        g['kill_check'] = lambda tool, sid: {'ok': True, 'pids': [p.pid], 'reason': 'exact',
                                             'ident': {p.pid: real}}
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BATON_TEST_PROCS', None)
            g['kill_session']('claude', 'x', str(p.pid))
        time.sleep(0.2)
        self.assertIsNone(p.poll(), 'the pid changed identity after preflight: never signal it')

    def test_kill_preflights_every_pid_before_signalling_any(self):
        g = load_scanner(self.home)
        a = subprocess.Popen(['/bin/sleep', '600'])
        b = subprocess.Popen(['/bin/sleep', '600'])
        for q in (a, b):
            self.addCleanup(lambda q=q: (q.poll() is None and q.kill(), q.wait()))
        ident = {a.pid: g['_lstart'](a.pid), b.pid: 'Mon Jan  1 00:00:00 2001'}   # b doesn't match
        pids = sorted(ident)
        g['kill_check'] = lambda tool, sid: {'ok': True, 'pids': pids, 'reason': 'exact', 'ident': ident}
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BATON_TEST_PROCS', None)
            out = g['kill_session']('claude', 'x', ','.join(str(x) for x in pids))
        self.assertEqual(out['result'], 'changed')
        time.sleep(0.2)
        self.assertIsNone(a.poll(), 'nothing may be signalled when any pid fails preflight')
        self.assertIsNone(b.poll())

    def test_kill_fails_closed_when_ps_cannot_inspect(self):
        g = load_scanner(self.home)
        p = subprocess.Popen(['/bin/sleep', '600'])
        self.addCleanup(lambda: (p.poll() is None and p.kill(), p.wait()))
        real = g['_lstart'](p.pid)
        def broken(pid):
            raise g['InspectError']('ps failed')
        g['_lstart'] = broken
        g['kill_check'] = lambda tool, sid: {'ok': True, 'pids': [p.pid], 'reason': 'exact',
                                             'ident': {p.pid: real}}
        from unittest import mock
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('BATON_TEST_PROCS', None)
            out = g['kill_session']('claude', 'x', str(p.pid))
        self.assertEqual(out['result'], 'unknown')
        time.sleep(0.2)
        self.assertIsNone(p.poll(), 'a ps failure must not be read as "gone" or lead to a signal')

    def test_lstart_distinguishes_gone_from_alive(self):
        g = load_scanner(self.home)
        p = subprocess.Popen(['/bin/sleep', '600'])
        self.assertTrue(g['_lstart'](p.pid))
        p.kill(); p.wait()
        self.assertIsNone(g['_lstart'](p.pid))

    def test_unstable_parse_keeps_the_previous_good_entry(self):
        from unittest import mock
        with mock.patch.dict(os.environ, {'XDG_CACHE_HOME': str(self.home / '.cache')}):
            g = load_scanner(self.home)
            f = self.home / 'x.jsonl'
            f.write_text('{}\n')
            st = f.stat()
            c = g['Cache']()
            good = {'ai_title': 'Good'}
            c.old[str(f)] = {'fp': [st.st_size - 1, st.st_mtime_ns, st.st_ino], 'facts': good}
            facts = c.get(str(f), st, lambda path: {'ai_title': None, 'stable': False},
                          g['CLAUDE_CARRY'])
            self.assertEqual(facts['ai_title'], 'Good')              # carried over
            self.assertIs(c.new[str(f)]['facts'], good)              # old entry kept
            c.save()
            saved = json.loads((self.home / '.cache' / 'baton' / 'index.json').read_text())
            self.assertEqual(saved['files'][str(f)]['facts'], good)


class MoreLiveTests(LiveTests):

    def test_codex_remote_value_flags_are_skipped(self):
        cwd, newer, older = self.two_codex()
        self.proc(5010, [CODEX_BIN, '--remote', 'ws://h:1', 'exec', 'x'], cwd)
        self.proc(5011, [CODEX_BIN, '--remote-auth-token-env', 'TOK', 'review', 'x'], cwd)
        data = self.dump()
        for tid in (newer, older):
            self.assertEqual(self.find(data, tid, 'codex')['live'], '')

    # only the new test; don't rerun the inherited ones
    for _n in [n for n in dir(LiveTests) if n.startswith('test_')]:
        locals()[_n] = None
    del _n


class MoreRenderTests2(BatonCase):
    render = RenderTests.render

    def test_children_of_a_visible_superseded_parent_resume_it(self):
        cwd = self.work('webapp')
        a, b = new_uuid(), new_uuid()
        self.claude_session(cwd, c_basic(a, cwd, 'old') + [c_continued_in(a, b)], sid=a)
        self.claude_session(cwd, c_basic(b, cwd, 'new'), sid=b)
        self.claude_child(a, cwd, 'a8888888888888888', 'Plan', 'old child')
        rows = self.render(state='tool=both\nall=1\nexpanded=\nunfolded=claude%%3A%s\n' % a)
        child = [r for r in rows if r[6] == 'child']
        self.assertEqual([(r[1], r[2]) for r in child], [('claude', a)])

    def test_preview_reaches_a_question_9_mb_back(self):
        cwd = self.work('proj')
        sid = new_uuid()
        recs = [c_user(sid, cwd, 'the buried question')]
        for _ in range(90):
            recs.append(c_tool_result(sid, cwd))
            recs[-1]['message']['content'][0]['content'] = 'x' * 100000
        recs.append(c_assistant(sid, cwd, 'answer'))
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.assertGreater(path.stat().st_size, 8 * 1024 * 1024)
        p = subprocess.run([str(BATON), '--preview', str(path), 'claude', ''], env=self.env(),
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertIn('the buried question', p.stdout)


class PickerFlowTests(KillTests):
    """The real picker under a pty (needs fzf): a row that looked CLOSED when
    the list was drawn but went live afterwards still gets the kill prompt."""

    for _n in [n for n in dir(KillTests) if n.startswith('test_')]:
        locals()[_n] = None
    del _n

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf')
    def test_enter_revalidates_a_row_that_went_live_after_drawing(self):
        import pty, select
        cwd = self.work('proj')
        sid = new_uuid()
        self.claude_session(cwd, c_basic(sid, cwd, 'q', title='Was Closed'), sid=sid)
        (self.home / '.claude' / 'sessions').mkdir(parents=True)     # registry exists, empty
        env = self.real_env()
        env['TERM'] = 'xterm-256color'
        pid, fd = pty.fork()
        if pid == 0:
            import fcntl, struct, termios
            fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', 40, 150, 0, 0))
            os.chdir(cwd)
            os.execve(str(BATON), [str(BATON), '--emit'], env)
        buf = b''
        def read_until(needle, secs):
            nonlocal buf
            end = time.time() + secs
            while time.time() < end and needle not in buf:
                r, _, _ = select.select([fd], [], [], 0.2)
                if r:
                    try:
                        buf += os.read(fd, 65536)
                    except OSError:
                        break
            return needle in buf
        try:
            self.assertTrue(read_until(b'Was Closed', 20), buf[-2000:])
            p, lst = self.spawn_claude()                 # the session goes live NOW
            self.claude_registry(p.pid, sid, cwd, proc_start=lst, status='busy')
            os.write(fd, b'\r')                          # Enter on the (stale) row
            self.assertTrue(read_until(b'End that process and resume here?', 20), buf[-2000:])
            os.write(fd, b'n\r')
            read_until(b'cancelled', 10)
            self.assertIsNone(p.poll(), 'answering n must not end the process')
        finally:
            try:
                os.kill(pid, 9)
            except OSError:
                pass
            os.waitpid(pid, 0)



class InstallerTests(BatonCase):
    """scripts/install.sh rc wiring, in a sandbox HOME (never the real rc)."""

    def install(self, rc, extra_path=None):
        env = {'HOME': str(self.home), 'PATH': os.environ.get('PATH', '/usr/bin:/bin'),
               'BATON_INSTALL_DIR': str(self.home / 'share'), 'BATON_BIN_DIR': str(self.home / 'bin'),
               'BATON_RC_FILE': str(rc)}
        if extra_path:
            env['PATH'] = '%s:%s' % (extra_path, env['PATH'])
        return subprocess.run(['bash', str(REPO / 'scripts' / 'install.sh'), '--from-source', str(REPO)],
                              env=env, capture_output=True, text=True, timeout=TIMEOUT)

    def test_existing_block_is_replaced_in_place_and_idempotent(self):
        rc = self.home / 'bashrc'
        rc.write_text('before\n# >>> baton shell integration >>>\nold line\n'
                      '# <<< baton shell integration <<<\nafter\n')
        for _ in range(2):
            p = self.install(rc)
            self.assertEqual(p.returncode, 0, p.stderr)
        text = rc.read_text()
        self.assertEqual(text.count('# >>> baton shell integration >>>'), 1)
        self.assertNotIn('old line', text)
        self.assertIn('shell-init bash', text)
        self.assertTrue(text.startswith('before\n') and text.endswith('after\n'), text)

    def test_block_is_appended_when_absent(self):
        rc = self.home / 'zshrc'
        rc.write_text('export A=1\n')
        p = self.install(rc)
        self.assertEqual(p.returncode, 0, p.stderr)
        text = rc.read_text()
        self.assertTrue(text.startswith('export A=1\n'))
        self.assertIn('shell-init zsh', text)

    def test_a_failing_awk_never_truncates_the_rc(self):
        rc = self.home / 'bashrc'
        original = 'keep me\n# >>> baton shell integration >>>\nold\n# <<< baton shell integration <<<\n'
        rc.write_text(original)
        stub = self.home / 'stub-bin'
        stub.mkdir()
        (stub / 'awk').write_text('#!/bin/sh\nexit 2\n')
        (stub / 'awk').chmod(0o755)
        p = self.install(rc, extra_path=str(stub))
        self.assertEqual(rc.read_text(), original, 'rc must be left unchanged')
        self.assertIn('left it unchanged', p.stderr)


if __name__ == '__main__':
    unittest.main()
