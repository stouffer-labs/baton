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
import calendar
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import uuid
from pathlib import Path
from urllib.parse import quote

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

    def test_launch_folder_beats_a_live_folder(self):
        live_dir = self.work('live')
        here = self.work('here')
        now = time.time()
        s1, s2 = new_uuid(), new_uuid()
        self.claude_session(live_dir, c_basic(s1, live_dir, 'x', title='Live Title'), sid=s1, mtime=now - 10)
        self.claude_session(here, c_basic(s2, here, 'y', title='Old Title Here'), sid=s2, mtime=now - 90000)
        self.claude_registry(4100, s1, live_dir)
        self.proc(4100, [CLAUDE_BIN], live_dir)
        rows = self.render(extra_env={'BATON_ORIGIN': here})
        folders = [r[4] for r in rows if r[6] == 'folder']
        self.assertEqual(folders, [here, live_dir])
        self.assertEqual([r[6] for r in rows[:3]], ['status', 'folder', 'session'])
        self.assertEqual(rows[2][2], s2)
        rows = self.render('title', extra_env={'BATON_ORIGIN': here})   # search: both match, here first
        self.assertEqual([r[4] for r in rows if r[6] == 'folder'], [here, live_dir])

    def test_root_origin_is_preserved(self):
        sid = new_uuid()
        self.claude_session('/', c_basic(sid, '/', 'at root', title='Root Session'), sid=sid)
        rows = self.render(extra_env={'BATON_ORIGIN': '/'})
        folders = [r for r in rows if r[6] == 'folder']
        self.assertEqual([r[4] for r in folders], ['/'])
        self.assertIn('here', folders[0][9])
        self.assertNotIn('no sessions here yet', folders[0][9])

    def test_launch_folder_hidden_by_filter_says_so(self):
        here = self.work('here')
        tid, row = self.codex_thread(here, name='Codex Only')
        self.codex_db([row])
        rows = self.render(state='tool=claude\nall=0\nexpanded=\nunfolded=\n',
                           extra_env={'BATON_ORIGIN': here})
        hdr = [r for r in rows if r[6] == 'folder'][0]
        self.assertEqual(hdr[4], here)
        self.assertIn('for this filter', hdr[9])
        self.assertNotIn('no sessions here yet', hdr[9])
        p = subprocess.run([str(BATON), '--preview', '', '', hdr[8]], env=self.env(),
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertIn('current view hides them', p.stdout)

    def test_launch_folder_without_sessions_shows_an_empty_header(self):
        a = self.work('aaa')
        nowhere = self.work('nowhere')
        sid = new_uuid()
        self.claude_session(a, c_basic(sid, a, 'x', title='In A'), sid=sid)
        rows = self.render(extra_env={'BATON_ORIGIN': nowhere})
        self.assertEqual([r[6] for r in rows[:4]], ['status', 'folder', 'folder', 'session'])
        self.assertEqual(rows[1][4], nowhere)
        self.assertIn('no sessions here yet', rows[1][9])
        self.assertEqual(rows[1][2], '')                     # not selectable
        self.assertEqual(rows[2][4], a)
        p = subprocess.run([str(BATON), '--preview', '', '', rows[1][8]], env=self.env(),
                           capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
        self.assertIn('No claude or codex session was started here yet', p.stdout)

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


# ======================================================================
# Watch mode: the read-only live view (--watch-session)
# ======================================================================

ANSI_RE = re.compile(r'\x1b\[[0-9;?]*[ -/]*[@-~]')
# Where tests look for system tools: CI runs one job with PATH=/bin:/usr/bin
# only, and lsof lives in /usr/sbin on macOS.
SYSTEM_PATH = os.pathsep.join([os.environ.get('PATH', ''), '/usr/sbin', '/usr/bin', '/bin', '/sbin'])


def c_block(sid, cwd, block, ts='2026-09-27T10:00:05.000Z', **extra):
    """An assistant line holding ONE content block, as Claude 2.1.295 writes
    them (thinking, text and each tool_use land on separate lines)."""
    d = {'parentUuid': new_uuid(), 'isSidechain': False, 'type': 'assistant',
         'message': {'id': 'msg_%s' % new_uuid()[:8], 'type': 'message', 'role': 'assistant',
                     'model': 'claude-opus-5-5', 'content': [block], 'stop_reason': None},
         'requestId': 'req_%s' % new_uuid()[:8], 'uuid': new_uuid(), 'timestamp': ts,
         'userType': 'external', 'entrypoint': 'cli', 'cwd': cwd, 'sessionId': sid,
         'version': '2.1.295', 'gitBranch': 'main'}
    d.update(extra)
    return d


def c_tool_use(sid, cwd, name, inp, tid, ts='2026-09-27T10:00:05.000Z'):
    return c_block(sid, cwd, {'type': 'tool_use', 'id': tid, 'name': name, 'input': inp}, ts=ts)


def c_result(sid, cwd, tid, content, is_error=None, ts='2026-09-27T10:00:06.000Z'):
    block = {'type': 'tool_result', 'tool_use_id': tid, 'content': content}
    if is_error is not None:
        block['is_error'] = is_error
    return {'parentUuid': new_uuid(), 'isSidechain': False, 'type': 'user',
            'message': {'role': 'user', 'content': [block]}, 'uuid': new_uuid(),
            'timestamp': ts, 'userType': 'external', 'entrypoint': 'cli', 'cwd': cwd,
            'sessionId': sid, 'version': '2.1.295', 'gitBranch': 'main',
            'toolUseResult': {'stdout': '', 'stderr': '', 'interrupted': False},
            'sourceToolAssistantUUID': new_uuid()}


def c_sys(sid, cwd, subtype, **extra):
    d = {'parentUuid': new_uuid(), 'isSidechain': False, 'type': 'system', 'subtype': subtype,
         'uuid': new_uuid(), 'timestamp': '2026-09-27T10:00:07.000Z', 'userType': 'external',
         'entrypoint': 'cli', 'cwd': cwd, 'sessionId': sid, 'version': '2.1.295'}
    d.update(extra)
    return d


def x_rec(type_, payload, ts='2026-10-09T05:00:01.000Z'):
    return {'timestamp': ts, 'type': type_, 'payload': payload}


def x_meta(tid, cwd, source='cli'):
    return x_rec('session_meta', {
        'id': tid, 'session_id': tid, 'timestamp': '2026-10-09T05:00:00.000Z', 'cwd': cwd,
        'originator': 'codex-tui', 'cli_version': '0.162.0', 'source': source,
        'thread_source': 'user', 'model_provider': 'openai',
        'base_instructions': {'text': 'You are Codex.'}, 'history_mode': 'paginated',
        'context_window': 258400})


def x_msg(role, text, phase=None):
    kind = 'output_text' if role == 'assistant' else 'input_text'
    p = {'type': 'message', 'id': 'msg_%s' % new_uuid()[:8], 'role': role,
         'content': [{'type': kind, 'text': text}]}
    if phase:
        p['phase'] = phase
    return x_rec('response_item', p)


def x_item(item, ts='2026-10-09T05:00:02.000Z'):
    """event_msg item_completed (Codex 0.162 also logs every message here)."""
    return x_rec('event_msg', {'type': 'item_completed', 'thread_id': 't', 'turn_id': 'turn1',
                               'item': item, 'started_at_ms': 1791531600000,
                               'completed_at_ms': 1791531601000}, ts=ts)


def x_call(name, args, call_id, namespace=None, ts='2026-10-09T05:00:01.000Z'):
    p = {'type': 'function_call', 'id': 'fc_%s' % new_uuid()[:8], 'name': name,
         'arguments': json.dumps(args), 'call_id': call_id}
    if namespace:
        p['namespace'] = namespace
    return x_rec('response_item', p, ts=ts)


def x_out(call_id, output='ok', kind='function_call_output'):
    return x_rec('response_item', {'type': kind, 'id': 'fco_%s' % new_uuid()[:8],
                                   'call_id': call_id, 'output': output})


def x_custom(name, inp, call_id):
    return x_rec('response_item', {'type': 'custom_tool_call', 'id': 'ctc_%s' % new_uuid()[:8],
                                   'status': 'completed', 'call_id': call_id, 'name': name,
                                   'input': inp})


def x_event(ptype, **fields):
    return x_rec('event_msg', dict(fields, type=ptype))


class WatchCase(BatonCase):
    """`--watch-session PATH [CARD] --once` prints the header, the feed the
    live view would start with, and the status line, then exits."""

    def env(self):
        e = super().env()
        e.update(TZ='UTC', COLUMNS='120')
        return e

    def watch_once(self, path, card=None, check=True):
        args = ['--watch-session', str(path)] + ([card] if card is not None else []) + ['--once']
        return self.run_baton(*args, check=check)

    def feed(self, path, card=None):
        out = self.watch_once(path, card).stdout
        lines = [ANSI_RE.sub('', ln) for ln in out.splitlines()]
        self.assertGreaterEqual(len(lines), 2, out)
        return lines[1:-1], lines[0], lines[-1]

    def codex_raw_rollout(self, tid, records):
        d = self.codex / 'sessions' / '2026' / '10' / '09'
        d.mkdir(parents=True, exist_ok=True)
        path = d / ('rollout-2026-10-09T05-00-00-%s.jsonl' % tid)
        with open(path, 'w') as fh:
            for i, r in enumerate(records):
                fh.write(json.dumps(dict(r, ordinal=i)) + '\n')
        return path

    def registry_entry(self, pid, sid, cwd, **fields):
        d = self.home / '.claude' / 'sessions'
        d.mkdir(parents=True, exist_ok=True)
        rec = {'pid': pid, 'sessionId': sid, 'cwd': cwd, 'startedAt': 1790477799738,
               'procStart': LSTART, 'version': '2.1.295', 'peerProtocol': 1, 'kind': 'interactive',
               'entrypoint': 'cli', 'pidDomain': 'darwin', 'name': 'n', 'nameSource': 'auto',
               'status': 'busy', 'updatedAt': 1790577944912, 'statusUpdatedAt': 1790577944912}
        rec.update(fields)
        (d / ('%d.json' % pid)).write_text(json.dumps(rec))


class WatchFeedTests(WatchCase):

    def test_claude_feed_shows_prompts_replies_tool_starts_and_failures(self):
        cwd = self.work('proj')
        sid = new_uuid()
        recs = [
            {'type': 'queue-operation', 'operation': 'enqueue', 'timestamp': '2026-09-27T10:00:00.000Z',
             'sessionId': sid, 'content': 'fix the login redirect'},
            c_user(sid, cwd, 'fix the login redirect'),
            c_block(sid, cwd, {'type': 'thinking', 'thinking': 'secret plan', 'signature': 'sig'}),
            c_block(sid, cwd, {'type': 'text', 'text': 'Fixing it now.'}),
            c_tool_use(sid, cwd, 'Bash', {'command': 'npm run e2e', 'description': 'Run e2e suite'}, 'toolu_A'),
            c_result(sid, cwd, 'toolu_A', 'all passed', is_error=False),
            c_tool_use(sid, cwd, 'Edit', {'file_path': cwd + '/src/a.ts', 'old_string': 'x',
                                          'new_string': 'y'}, 'toolu_B'),
            c_result(sid, cwd, 'toolu_B',
                     '<tool_use_error>String to replace not found in file.</tool_use_error>', is_error=True),
            c_tool_use(sid, cwd, 'mcp__playwright__browser_click', {'element': 'Sign in'}, 'toolu_C'),
            c_result(sid, cwd, 'toolu_C', [{'type': 'text', 'text': 'Error: only looks like one'}]),
            c_attachment(sid, cwd, 10),
            c_block(sid, cwd, {'type': 'text', 'text': 'Done.'}),
            c_last_prompt(sid, 'fix the login redirect'),
        ]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, header, status = self.feed(path)
        self.assertEqual(feed, [
            'You:    fix the login redirect',
            'Claude: Fixing it now.',
            '  › Bash  Run e2e suite',
            '  › Edit  ~/work/proj/src/a.ts',
            '  ✗ Edit  String to replace not found in file.',
            '  › playwright › browser_click  {"element":"Sign in"}',
            'Claude: Done.',
        ])
        self.assertNotIn('secret plan', '\n'.join([header, status] + feed))

    def test_claude_feed_notes_compaction_api_errors_and_interrupts(self):
        cwd = self.work('proj')
        sid = new_uuid()
        api_msg = c_block(sid, cwd, {'type': 'text', 'text': 'API Error: 503 Service Unavailable'},
                          isApiErrorMessage=True, error='server_error', apiErrorStatus=503)
        api_msg['message']['model'] = '<synthetic>'
        retry = {'level': 'error', 'retryInMs': 559, 'maxRetries': 10, 'source': 'request_retry',
                 'error': {'message': 'Connection error.', 'formatted': "Can't reach the API server"}}
        recs = [
            c_user(sid, cwd, 'start'),
            c_block(sid, cwd, {'type': 'text', 'text': 'Working.'}),
            c_sys(sid, cwd, 'api_error', retryAttempt=1, **retry),
            c_sys(sid, cwd, 'api_error', retryAttempt=2, **retry),      # same failure again: not repeated
            api_msg,
            c_sys(sid, cwd, 'compact_boundary', content='Conversation compacted',
                  compactMetadata={'trigger': 'auto', 'preTokens': 150000}),
            c_user(sid, cwd, 'This session is being continued from a previous conversation.',
                   isCompactSummary=True, isVisibleInTranscriptOnly=True),
            c_user(sid, cwd, [{'type': 'text', 'text': '[Request interrupted by user]'}]),
            c_user(sid, cwd, 'Caveat: the messages below were generated by local commands.', isMeta=True),
            c_user(sid, cwd, BUILTIN_MODEL_CMD),
            c_user(sid, cwd, 'background task finished', promptSource='system',
                   turnOrigin='task_notification'),
        ]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, _, _ = self.feed(path)
        self.assertEqual(feed, [
            'You:    start',
            'Claude: Working.',
            "  ✗ API error (retry 1/10): Can't reach the API server",
            '  ✗ API Error: 503 Service Unavailable',
            '  ── conversation compacted ──',
            '  ── interrupted ──',
            'You:    /model',
        ])

    def test_feed_starts_at_the_second_to_last_prompt(self):
        cwd = self.work('proj')
        sid = new_uuid()
        recs = []
        for i in range(1, 5):
            recs += [c_user(sid, cwd, 'question %d' % i), c_block(sid, cwd, {'type': 'text', 'text': 'answer %d' % i})]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, _, _ = self.feed(path)
        self.assertEqual(feed, ['You:    question 3', 'Claude: answer 3', 'You:    question 4', 'Claude: answer 4'])

    def test_watch_strips_escape_sequences_from_transcript_text(self):
        cwd = self.work('proj')
        sid = new_uuid()
        recs = [c_user(sid, cwd, 'hi \x1b]0;pwned\x07there'),
                c_tool_use(sid, cwd, 'Bash', {'command': 'x', 'description': 'wipe \x1b[2J screen'}, 'toolu_X'),
                c_block(sid, cwd, {'type': 'text', 'text': 'ok \x1b[31mred\x1b[0m \x9b1m'})]
        _, path = self.claude_session(cwd, recs, sid=sid)
        out = self.watch_once(path).stdout
        self.assertIn('hi there', out)
        self.assertIn('wipe screen', out)
        self.assertIsNone(CONTROL_RE.search(out.replace('\n', '')), repr(out))

    def test_codex_feed_uses_one_message_channel_and_shows_tool_outcomes(self):
        cwd = self.work('svc')
        tid = new_uuid()
        patch = '*** Begin Patch\n*** Update File: src/a.ts\n@@\n-x\n+y\n*** Add File: src/b.ts\n+z\n*** End Patch'
        recs = [
            x_meta(tid, cwd),
            x_msg('developer', '<permissions instructions>sandboxed</permissions instructions>'),
            x_msg('user', '<environment_context>\n  <cwd>%s</cwd>\n</environment_context>' % cwd),
            x_msg('user', 'fix the build'),
            x_item({'type': 'UserMessage', 'id': new_uuid(), 'content': [{'type': 'text', 'text': 'fix the build'}]}),
            x_event('task_started', turn_id='turn1', started_at=1791531600, model_context_window=258400),
            x_rec('response_item', {'type': 'reasoning', 'summary': [], 'encrypted_content': 'zz', 'content': None}),
            x_msg('assistant', 'Checking the tests.', phase='commentary'),
            x_item({'type': 'AgentMessage', 'id': new_uuid(), 'phase': 'commentary',
                    'content': [{'type': 'text', 'text': 'Checking the tests.'}]}),
            x_call('exec_command', {'cmd': 'npm test', 'workdir': cwd}, 'call_1'),
            x_item({'type': 'CommandExecution', 'id': 'call_1', 'command': ["/bin/zsh -lc 'npm test'"],
                    'cwd': cwd, 'status': 'failed', 'exit_code': 1, 'aggregated_output': '2 tests failed\nmore',
                    'duration': {'secs': 3, 'nanos': 0}}),
            x_out('call_1', 'Process exited with code 1'),
            x_rec('response_item', {'type': 'web_search_call', 'id': 'ws_1', 'status': 'completed',
                                    'action': {'type': 'search', 'query': 'vitest config',
                                               'queries': ['vitest config']}}),
            x_custom('apply_patch', patch, 'call_2'),
            x_item({'type': 'FileChange', 'id': 'call_2', 'status': 'completed',
                    'changes': {cwd + '/src/a.ts': {'type': 'update'}}}),
            x_out('call_2', 'Success', kind='custom_tool_call_output'),
            x_custom('apply_patch', '*** Begin Patch\n*** Delete File: old.ts\n*** End Patch', 'call_4'),
            x_item({'type': 'FileChange', 'id': 'call_4', 'status': 'failed', 'stderr': 'no such file',
                    'changes': {cwd + '/old.ts': {'type': 'delete'}}}),
            x_call('email_search', {'query': 'invoice'}, 'call_3', namespace='mcp__mail_mcp'),
            x_item({'type': 'McpToolCall', 'id': 'call_3', 'server': 'mail_mcp', 'tool': 'email_search',
                    'arguments': {'query': 'invoice'}, 'status': 'failed',
                    'result': {'content': [{'type': 'text', 'text': 'token expired'}], 'isError': True}}),
            x_out('call_3', 'token expired'),
            x_msg('assistant', 'All green now.', phase='final_answer'),
            x_event('task_complete', turn_id='turn1', last_agent_message='All green now.',
                    started_at=1791531600, completed_at=1791531700, duration_ms=100000),
            x_event('turn_aborted', turn_id='turn2', reason='interrupted'),
            x_event('token_count', info=None),
            x_rec('turn_context', {'cwd': cwd, 'model': 'gpt-5'}),
        ]
        path = self.codex_raw_rollout(tid, recs)
        feed, _, _ = self.feed(path)
        self.assertEqual(feed, [
            'You:    fix the build',
            'Codex:  Checking the tests.',
            '  › shell  npm test',
            '  ✗ shell  exit 1: 2 tests failed',
            '  › web_search  vitest config',
            '  › apply_patch  src/a.ts, src/b.ts',
            '  › apply_patch  old.ts',
            '  ✗ apply_patch  failed: no such file',
            '  › mail_mcp › email_search  {"query":"invoice"}',
            '  ✗ mail_mcp › email_search  token expired',
            'Codex:  All green now.',
            '  ── turn aborted (interrupted) ──',
        ])

    def test_watching_a_subagent_row_follows_the_subagent_file(self):
        cwd = self.work('proj')
        parent = new_uuid()
        self.claude_session(cwd, c_basic(parent, cwd, 'the parent prompt', title='Parent'), sid=parent)
        child = self.claude_child(parent, cwd, 'a1', 'Explore', 'scan the repo')
        feed, _, _ = self.feed(child)
        self.assertEqual(feed, ['Task:   Task: scan the repo', 'Claude: done'])

    def test_unknown_and_malformed_records_are_skipped(self):
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, [c_user(sid, cwd, 'q'), {'type': 'brand-new-kind', 'x': 1},
                                            'a json string', [1, 2],
                                            c_block(sid, cwd, {'type': 'future_block', 'z': 1}),
                                            c_block(sid, cwd, {'type': 'text', 'text': 'a'})], sid=sid,
                                      raw_tail='{"type": "user", broken\n')
        feed, _, _ = self.feed(path)
        self.assertEqual(feed, ['You:    q', 'Claude: a'])


    def test_api_error_repeats_after_the_session_recovered(self):
        cwd = self.work('proj')
        sid = new_uuid()
        err = dict(level='error', retryAttempt=1, maxRetries=10,
                   error={'message': 'Connection error.', 'formatted': 'offline'})
        recs = [c_user(sid, cwd, 'go'), c_sys(sid, cwd, 'api_error', **err),
                c_block(sid, cwd, {'type': 'text', 'text': 'back online'}),
                c_sys(sid, cwd, 'api_error', **err)]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, _, _ = self.feed(path)
        self.assertEqual(sum('offline' in ln for ln in feed), 2, feed)

    def test_every_transcript_string_is_sanitized(self):
        cwd = self.work('proj')
        sid = new_uuid()
        evil = 'x\x1b[2Jy\x07\x9b2J'
        rec = c_user(sid, cwd, 'hello')
        rec['cwd'] = cwd + evil                          # the header's fallback folder
        recs = [rec,
                c_tool_use(sid, cwd, 'mcp__s%s__t%s' % (evil, evil), {'k': evil}, 'toolu_1'),
                c_result(sid, cwd, 'toolu_1', 'bad ' + evil, is_error=True),
                c_sys(sid, cwd, 'api_error', error={'formatted': evil}, retryAttempt=1, maxRetries=2)]
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.registry_entry(4100, sid, cwd, status='waiting', waitingFor='weird ' + evil)
        self.proc(4100, [CLAUDE_BIN], cwd)
        card = quote(json.dumps({'t': 'title ' + evil}), safe='')
        tid = 'abcd\x1b[2Jefgh-0000-0000-0000-000000000000'
        xpath = self.codex_raw_rollout('x' + new_uuid(), [
            x_meta(tid, cwd + evil), x_msg('user', 'q ' + evil),
            x_call('exec_command', {'cmd': 'ls ' + evil}, 'c1'),
            x_item({'type': 'CommandExecution', 'id': 'c1', 'status': 'failed', 'exit_code': 2,
                    'aggregated_output': 'out ' + evil})])
        for args in ((path, card), (path, None), (xpath, None)):
            with self.subTest(path=str(args[0]), card=bool(args[1])):
                out = self.watch_once(*args).stdout
                self.assertIsNone(CONTROL_RE.search(out.replace('\n', '')), repr(out))

    def test_tools_finishing_out_of_order_leave_the_older_one_running(self):
        cwd = self.work('proj')
        sid = new_uuid()
        recs = [c_user(sid, cwd, 'go'),
                c_tool_use(sid, cwd, 'Bash', {'command': 'slow'}, 'toolu_1', ts='2026-10-09T10:01:00.000Z'),
                c_tool_use(sid, cwd, 'Read', {'file_path': '/f'}, 'toolu_2', ts='2026-10-09T10:02:00.000Z'),
                c_result(sid, cwd, 'toolu_2', 'ok')]
        _, path = self.claude_session(cwd, recs, sid=sid)
        self.registry_entry(4100, sid, cwd, status='busy')
        self.proc(4100, [CLAUDE_BIN], cwd)
        _, _, status = self.feed(path)
        self.assertIn('running Bash since 10:01', status)

    def test_backfill_reaches_the_second_to_last_prompt_past_8_mb(self):
        cwd = self.work('proj')
        sid = new_uuid()
        filler = [c_result(sid, cwd, 'toolu_%d' % i, 'z' * (3 * 1024 * 1024)) for i in range(4)]
        recs = [c_user(sid, cwd, 'the earlier question')] + filler + [c_user(sid, cwd, 'the latest question')]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, _, _ = self.feed(path)
        self.assertEqual(feed[0], 'You:    the earlier question', feed[:3])

    def test_a_backfill_cut_at_32_mb_says_so(self):
        cwd = self.work('proj')
        sid = new_uuid()
        filler = [c_result(sid, cwd, 'toolu_%d' % i, 'z' * (7 * 1024 * 1024)) for i in range(5)]
        recs = [c_user(sid, cwd, 'too far back')] + filler + [c_user(sid, cwd, 'the latest question')]
        _, path = self.claude_session(cwd, recs, sid=sid)
        feed, _, _ = self.feed(path)
        self.assertNotIn('too far back', '\n'.join(feed))
        self.assertEqual(feed[0], '  ── older activity not shown (last 32 MB only) ──', feed[:3])
        self.assertIn('You:    the latest question', feed)

    def test_a_record_starting_exactly_at_the_window_edge_is_kept(self):
        cwd = self.work('proj')
        sid = new_uuid()
        def line(r):
            return (json.dumps(r) + '\n').encode()
        head = line(c_user(sid, cwd, 'way back')) + line(c_attachment(sid, cwd, 4096))
        body = (line(c_tool_use(sid, cwd, 'Bash', {'command': 'edge'}, 'toolu_edge', ts='2026-10-09T11:11:00.000Z'))
                + line(c_user(sid, cwd, 'second to last')) + line(c_user(sid, cwd, 'last')))
        pad = line(c_attachment(sid, cwd, 0))
        pad = line(c_attachment(sid, cwd, 2 * 1024 * 1024 - len(body) - len(pad)))
        self.assertEqual(len(body) + len(pad), 2 * 1024 * 1024)
        _, path = self.claude_session(cwd, [], sid=sid)
        path.write_bytes(head + body + pad)
        self.registry_entry(4100, sid, cwd, status='busy')
        self.proc(4100, [CLAUDE_BIN], cwd)
        _, _, status = self.feed(path)
        self.assertIn('running Bash since 11:11', status)

    def test_codex_tool_in_flight_without_a_turn_start_still_reads_busy(self):
        cwd = self.work('svc')
        tid = new_uuid()
        path = self.codex_raw_rollout(tid, [x_meta(tid, cwd), x_msg('user', 'go'),
                                            x_call('exec_command', {'cmd': 'make'}, 'c1',
                                                   ts='2026-10-09T08:30:00.000Z')])
        self.proc(5100, [CODEX_BIN], cwd, open_files=[self.codex_lock(tid)])
        _, _, status = self.feed(path)
        self.assertIn('● busy', status)
        self.assertIn('running shell since 08:30', status)


class WatchTargetTests(WatchCase):

    def test_a_path_outside_the_session_roots_is_refused(self):
        outside = self.home / 'notes.jsonl'
        outside.write_text('{}\n')
        link_dir = self.projects / '-x'
        link_dir.mkdir()
        link = link_dir / ('%s.jsonl' % new_uuid())
        link.symlink_to(outside)
        for p in (outside, link):
            with self.subTest(path=str(p)):
                r = self.watch_once(p, check=False)
                self.assertEqual(r.returncode, 2, r.stderr)
                self.assertEqual(r.stdout, '')
                self.assertIn('not a claude or codex session file', r.stderr)

    def test_header_shows_the_card_title_and_status_says_not_running(self):
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, c_basic(sid, cwd, 'q'), sid=sid,
                                      mtime=calendar.timegm((2026, 10, 9, 5, 2, 30)))
        card = quote(json.dumps({'t': 'Fix Login', 'tool': 'claude', 'cwd': cwd}), safe='')
        _, header, status = self.feed(path, card)
        self.assertIn('unknown', status, 'without a registry baton cannot tell')
        (self.home / '.claude' / 'sessions').mkdir(parents=True)
        _, header, status = self.feed(path, card)
        self.assertIn('Fix Login', header)
        self.assertIn('claude', header)
        self.assertIn('~/work/proj', header)
        self.assertIn('read-only', header)
        self.assertIn('not running', status)
        self.assertIn('05:02', status)


class WatchStatusTests(WatchCase):

    def live_claude(self, recs, **reg):
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, [c_user(sid, cwd, 'go')] + [r(sid, cwd) for r in recs], sid=sid)
        self.registry_entry(4100, sid, cwd, **reg)
        self.proc(4100, [CLAUDE_BIN], cwd)
        return path

    def test_waiting_status_names_what_claude_waits_for(self):
        path = self.live_claude([], status='waiting', waitingFor='permission prompt')
        _, _, status = self.feed(path)
        self.assertIn('● waiting · permission prompt', status)

    def test_busy_status_names_the_running_tool_and_its_start_time(self):
        path = self.live_claude([
            lambda s, c: c_tool_use(s, c, 'Read', {'file_path': '/x'}, 'toolu_1', ts='2026-10-09T13:58:00.000Z'),
            lambda s, c: c_result(s, c, 'toolu_1', 'ok'),
            lambda s, c: c_tool_use(s, c, 'Bash', {'command': 'npm test'}, 'toolu_2', ts='2026-10-09T14:02:10.000Z'),
        ], status='busy')
        _, _, status = self.feed(path)
        self.assertIn('● busy', status)
        self.assertIn('running Bash since 14:02', status)
        self.assertNotIn('Read', status)

    def test_a_registry_entry_for_a_dead_pid_is_not_live(self):
        path = self.live_claude([], status='busy')
        self.procs.clear()                                   # pid 4100 is gone
        _, _, status = self.feed(path)
        self.assertIn('not running', status)
        self.assertNotIn('●', status)

    def test_codex_status_follows_turn_events_of_the_proven_holder(self):
        cwd = self.work('svc')
        tid = new_uuid()
        base = [x_meta(tid, cwd), x_msg('user', 'go'),
                x_event('task_started', turn_id='turn1', started_at=1791531600),
                x_call('exec_command', {'cmd': 'make'}, 'call_1', ts='2026-10-09T09:15:00.000Z')]
        self.proc(5100, [CODEX_BIN, 'resume', tid], cwd, open_files=[self.codex_lock(tid)])
        path = self.codex_raw_rollout(tid, base)
        _, _, status = self.feed(path)
        self.assertIn('● busy', status)
        self.assertIn('running shell since 09:15', status)
        path.unlink()
        path = self.codex_raw_rollout(tid, base + [x_out('call_1'),
                                                   x_event('task_complete', turn_id='turn1')])
        _, _, status = self.feed(path)
        self.assertIn('● idle', status)
        self.assertNotIn('running', status)
        self.procs.clear()                                   # nobody holds the lock now
        _, _, status = self.feed(path)
        self.assertIn('not running', status)


class Pty:
    """A real pseudo-terminal running one command: the watcher's /dev/tty."""

    def __init__(self, argv, env, cwd, cols=100, rows=30):
        import pty
        self.pid, self.fd = pty.fork()
        if self.pid == 0:                     # child: never returns
            try:
                import fcntl, struct, termios
                fcntl.ioctl(0, termios.TIOCSWINSZ, struct.pack('HHHH', rows, cols, 0, 0))
                os.chdir(cwd)
                os.execve(argv[0], argv, env)
            finally:
                os._exit(127)
        self.buf, self.status = b'', None

    def _read(self, timeout):
        import select
        r, _, _ = select.select([self.fd], [], [], timeout)
        if not r:
            return True
        try:
            data = os.read(self.fd, 65536)
        except OSError:
            return False
        self.buf += data
        return bool(data)

    def read_until(self, needle, secs=10, start=0):
        """Wait for `needle` in the output: a str is matched against the text
        with color codes stripped, bytes against the raw stream."""
        def seen():
            return (needle in self.text(start)) if isinstance(needle, str) else (needle in self.buf[start:])
        end = time.time() + secs
        while time.time() < end and not seen():
            if not self._read(0.1):
                break
        return seen()

    def pump(self, secs):
        end = time.time() + secs
        while time.time() < end and self._read(0.1):
            pass

    def send(self, data):
        os.write(self.fd, data)

    def wait(self, secs=5):
        end = time.time() + secs
        while time.time() < end:
            pid, st = os.waitpid(self.pid, os.WNOHANG)
            if pid:
                self.status = st
                self.pump(0.2)
                return st
            self._read(0.05)
        return None

    def text(self, start=0):
        return ANSI_RE.sub('', self.buf[start:].decode('utf-8', 'replace'))

    def close(self):
        # Close our side first: on macOS a dying session leader can block in
        # the kernel draining output nobody reads, and waitpid then never returns.
        try:
            os.close(self.fd)
        except OSError:
            pass
        if self.status is None:
            try:
                os.kill(self.pid, 9)
            except OSError:
                pass
            try:
                os.waitpid(self.pid, 0)
            except ChildProcessError:
                pass


class WatchLiveCase(WatchCase):
    """The interactive watcher under a pty, plus real-process helpers."""

    def watch_pty(self, path, env=None, cols=100, rows=30, wrapper=None):
        env = dict(env or self.env(), TERM='xterm-256color')
        argv = [str(BATON), '--watch-session', str(path)]
        if wrapper:
            import shlex
            argv = ['/bin/bash', '-c', wrapper % ' '.join(shlex.quote(a) for a in argv)]
        p = Pty(argv, env, str(self.home), cols, rows)
        self.addCleanup(p.close)
        return p

    def write_procs(self):
        (self.home / 'procs.json').write_text(json.dumps(self.procs))

    @staticmethod
    def append(path, *records, raw=b''):
        with open(str(path), 'ab') as fh:
            for r in records:
                fh.write((json.dumps(r) + '\n').encode())
            fh.write(raw)

    def quit(self, p, key=b'q'):
        p.send(key)
        st = p.wait(5)
        self.assertIsNotNone(st, 'the watcher did not stop on %r\n%s' % (key, p.text()[-1500:]))
        self.assertTrue(os.WIFEXITED(st) and os.WEXITSTATUS(st) == 0, (st, p.text()[-1500:]))

    def spawn_fake(self, name):
        """A real process whose argv[0] basename is `name` (it is /bin/sleep)."""
        p = subprocess.Popen([str(self.home / 'bin' / name), '600'], executable='/bin/sleep',
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (p.kill(), p.wait()))
        time.sleep(0.2)
        lst = subprocess.run(['ps', '-o', 'lstart=', '-p', str(p.pid)], capture_output=True, text=True,
                             env=dict(os.environ, TZ='UTC', LC_ALL='C')).stdout.strip()
        return p, lst

    def real_env(self, path_dirs=None):
        e = self.env()
        e.pop('BATON_TEST_PROCS')
        if path_dirs is not None:
            e['PATH'] = ':'.join(path_dirs)
        return e

    def stub_path(self, *tools):
        """A PATH dir with only the named tools (plus what baton itself needs)."""
        d = self.home / ('stub-%s' % new_uuid()[:6])
        d.mkdir()
        for name in ('bash', 'basename') + tools:
            src = sys.executable if name == 'python3' else shutil.which(name, path=SYSTEM_PATH)
            if not src:
                self.skipTest('%s is not installed' % name)
            (d / name).symlink_to(src)
        return str(d)

    def claude_live(self, prompt='go'):
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, [c_user(sid, cwd, prompt)], sid=sid)
        self.registry_entry(4100, sid, cwd)
        self.proc(4100, [CLAUDE_BIN], cwd)
        return cwd, sid, path


class WatchFollowTests(WatchLiveCase):

    def test_new_lines_stream_in_once_and_in_order(self):
        cwd, sid, path = self.claude_live('first question')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('first question'), p.text())
        mark = len(p.buf)
        self.append(path, c_block(sid, cwd, {'type': 'text', 'text': 'second message'}),
                    c_tool_use(sid, cwd, 'Bash', {'command': 'make deploy', 'description': 'deploy it'}, 'toolu_9'))
        self.assertTrue(p.read_until('deploy it', start=mark), p.text(mark))
        self.pump_and_quit(p)
        out = p.text(mark)
        self.assertEqual(out.count('second message'), 1, out)
        self.assertLess(out.index('second message'), out.index('deploy it'))
        self.assertIn(b'second message\r\n', p.buf, 'output processing (OPOST) must stay on')
        self.assertIn('Claude: second message', out, 'the colored label keeps its column')

    def pump_and_quit(self, p):
        p.pump(1.2)                                     # a few more polls: nothing repeats
        self.quit(p)

    def test_a_torn_line_waits_for_its_newline(self):
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        line = (json.dumps(c_block(sid, cwd, {'type': 'text', 'text': 'arrives in two parts'})) + '\n').encode()
        self.append(path, raw=line[:40])
        p.pump(1.5)
        self.assertNotIn('arrives in two parts', p.text())
        self.append(path, raw=line[40:])
        self.assertTrue(p.read_until('arrives in two parts'), p.text())
        self.quit(p)

    def test_a_character_split_across_writes_survives(self):
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        text = 'café ünïcode ✓ done'
        line = (json.dumps(c_block(sid, cwd, {'type': 'text', 'text': text}), ensure_ascii=False) + '\n').encode()
        cut = line.index('é'.encode()) + 1               # inside the two-byte é
        self.append(path, raw=line[:cut])
        p.pump(1.2)
        self.append(path, raw=line[cut:])
        self.assertTrue(p.read_until(text), p.text())
        self.quit(p)

    def test_a_huge_record_is_skipped_with_a_note(self):
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        self.append(path, c_user(sid, cwd, 'x' * (9 * 1024 * 1024)),
                    c_block(sid, cwd, {'type': 'text', 'text': 'after the huge one'}))
        self.assertTrue(p.read_until('after the huge one', secs=20), p.text()[-2000:])
        self.assertEqual(p.text().count('large entry skipped'), 1)
        self.assertNotIn('xxxxxxxx', p.text())
        self.quit(p)

    def test_truncation_shows_the_new_content_after_a_divider(self):
        cwd, sid, path = self.claude_live('before truncation')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('before truncation'))
        mark = len(p.buf)
        with open(str(path), 'w') as fh:
            fh.write(json.dumps(c_user(sid, cwd, 'rewritten from scratch')) + '\n')
        self.assertTrue(p.read_until('rewritten from scratch', start=mark), p.text(mark))
        self.assertIn('transcript restarted', p.text(mark))
        self.quit(p)

    def test_replacement_shows_the_new_file_after_a_divider(self):
        cwd, sid, path = self.claude_live('the old file')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('the old file'))
        mark = len(p.buf)
        tmp = str(path) + '.new'
        with open(tmp, 'w') as fh:
            fh.write(json.dumps(c_user(sid, cwd, 'the replacement file')) + '\n')
        os.replace(tmp, str(path))
        self.assertTrue(p.read_until('the replacement file', start=mark), p.text(mark))
        self.assertIn('transcript replaced', p.text(mark))
        self.quit(p)

    def test_a_briefly_missing_transcript_is_not_reported_gone(self):
        cwd, sid, path = self.claude_live('still here')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('still here'))
        data = path.read_bytes()
        path.unlink()
        p.pump(0.8)
        path.write_bytes(data)
        p.pump(2.5)
        self.assertNotIn('transcript gone', p.text())
        path.unlink()
        self.assertTrue(p.read_until('transcript gone', secs=8), p.text())
        self.quit(p)


class WatchKeyTests(WatchLiveCase):

    def test_q_ctrl_c_ctrl_d_and_esc_quit(self):
        for key in (b'q', b'\x03', b'\x04', b'\x1b'):
            with self.subTest(key=key):
                _, _, path = self.claude_live()
                p = self.watch_pty(path)
                self.assertTrue(p.read_until('read-only'))
                self.quit(p, key)

    def test_a_hangup_ends_the_watcher_without_hanging(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        p.send(b'\x1b')                                 # mid-sequence when the line drops
        os.close(p.fd)
        end = time.time() + 8
        while time.time() < end:
            pid, st = os.waitpid(p.pid, os.WNOHANG)
            if pid:
                p.status = st
                break
            time.sleep(0.1)
        self.assertIsNotNone(p.status, 'the watcher must exit when its terminal goes away')

    def test_an_arrow_key_does_not_quit(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        p.send(b'\x1b[A')
        self.assertIsNone(p.wait(1.5), 'an arrow key must not stop the watcher')
        self.quit(p)

    def test_esc_followed_by_q_in_one_read_quits(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        self.quit(p, b'\x1bq')

    def test_an_arrow_key_split_across_reads_does_not_quit(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        p.send(b'\x1b')
        time.sleep(0.1)
        p.send(b'[A')
        self.assertIsNone(p.wait(1.5), 'Esc followed by [A within 300 ms is an arrow key')
        self.quit(p)

    def test_nothing_is_redrawn_while_nothing_changes(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'))
        p.pump(2.6)                                      # past one liveness check
        n = len(p.buf)
        p.pump(4.5)                                      # two more checks, same state
        self.assertEqual(len(p.buf), n, repr(p.buf[n:]))
        self.quit(p)

    def test_ctrl_o_inside_the_view_does_not_swallow_output(self):
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('read-only'))
        p.send(b'\x0f')                                  # VDISCARD on macOS when IEXTEN is on
        p.pump(0.5)
        self.append(path, c_block(sid, cwd, {'type': 'text', 'text': 'still visible after ctrl-o'}))
        self.assertTrue(p.read_until('still visible after ctrl-o'), p.text())
        self.quit(p)

    def stty_flags(self, p):
        self.assertTrue(p.read_until('STTY>', secs=8), p.text()[-1500:])
        self.assertTrue(p.read_until('werase', secs=8), p.text()[-1500:])   # the end of `stty -a`
        p.wait(5)
        out = p.text()
        return out[out.index('STTY>'):]

    def test_terminal_settings_are_restored_on_quit_and_on_sigterm(self):
        import signal
        for how in ('q', 'term'):
            with self.subTest(how=how):
                _, _, path = self.claude_live()
                p = self.watch_pty(path, wrapper="trap '' TERM; %s; printf 'STTY>'; stty -a")
                self.assertTrue(p.read_until('read-only'))
                if how == 'q':
                    p.send(b'q')
                else:
                    os.killpg(p.pid, signal.SIGTERM)
                flags = self.stty_flags(p)
                self.assertRegex(p.buf, rb'(\r\x1b\[2K|\n)STTY>', 'the status line is cleared on exit')
                for f in ('icanon', 'echo', 'isig', 'iexten', 'ixon', 'icrnl'):
                    self.assertRegex(flags, r'(?<![-\w])%s\b' % f, flags)
                    self.assertNotRegex(flags, r'-%s\b' % f, flags)

    def test_status_line_fits_after_a_resize(self):
        import fcntl, signal, struct, termios
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path, cols=120)
        self.assertTrue(p.read_until('read-only'))
        fcntl.ioctl(p.fd, termios.TIOCSWINSZ, struct.pack('HHHH', 30, 40, 0, 0))
        os.killpg(p.pid, signal.SIGWINCH)
        p.pump(0.8)
        mark = len(p.buf)
        self.append(path, c_block(sid, cwd, {'type': 'text', 'text': 'after resize'}))
        self.assertTrue(p.read_until('after resize', start=mark))
        p.pump(0.6)
        last = p.buf.rsplit(b'\x1b[2K', 1)[-1].decode('utf-8', 'replace')
        status = ANSI_RE.sub('', last).replace('\r', '').rsplit('\n', 1)[-1]
        self.assertIn('q stop', status)
        self.assertLessEqual(len(status), 39, repr(status))
        self.quit(p)


class WatchLiveStateTests(WatchLiveCase):

    def test_status_follows_the_registry_busy_waiting_idle(self):
        cwd, sid, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'), p.text())
        self.registry_entry(4100, sid, cwd, status='waiting', waitingFor='permission prompt')
        self.assertTrue(p.read_until('● waiting · permission prompt'), p.text())
        self.registry_entry(4100, sid, cwd, status='idle')
        self.assertTrue(p.read_until('● idle'), p.text())
        self.quit(p)

    def test_a_session_id_change_follows_the_new_transcript(self):
        cwd, sid, path = self.claude_live('before clear')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('before clear'))
        new = new_uuid()
        self.claude_session(cwd, [c_user(new, cwd, 'after clear')], sid=new)
        self.registry_entry(4100, new, cwd)
        self.assertTrue(p.read_until('after clear'), p.text())
        self.assertIn('session changed', p.text())
        self.quit(p)

    def test_parking_follows_the_background_job(self):
        cwd, sid, path = self.claude_live('interactive part')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('interactive part'))
        job = 'abcd1234'
        bg = job + new_uuid()[8:]
        self.claude_session(cwd, [c_user(bg, cwd, 'background part')], sid=bg)
        self.registry_entry(4200, bg, cwd, kind='bg', jobId=job)
        self.proc(4200, [CLAUDE_BIN], cwd)
        self.write_procs()
        self.registry_entry(4100, sid, cwd, status='idle', parkedJobId=job)
        self.assertTrue(p.read_until('background part'), p.text())
        self.assertIn('background job', p.text())
        self.quit(p)

    def test_a_reused_pid_with_a_stale_registry_is_never_followed(self):
        cwd, sid, path = self.claude_live('the real one')
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'), p.text())
        other = new_uuid()
        self.claude_session(cwd, [c_user(other, cwd, 'some other session')], sid=other)
        self.procs[0]['lstart'] = 'Mon Sep 28 11:11:11 2026'       # pid 4100 now belongs to someone else
        self.write_procs()
        self.registry_entry(4100, other, cwd)                       # and a stale file names another session
        self.assertTrue(p.read_until('session ended', secs=12), p.text())
        self.assertNotIn('some other session', p.text())
        self.quit(p)

    def test_lsof_exiting_nonzero_with_no_answer_is_unknown(self):
        cwd = self.work('svc')
        tid = new_uuid()
        path = self.codex_raw_rollout(tid, [x_meta(tid, cwd), x_msg('user', 'go')])
        self.spawn_fake('codex')
        d = self.stub_path('python3', 'ps')
        (Path(d) / 'lsof').write_text('#!/bin/sh\nexit 1\n')
        (Path(d) / 'lsof').chmod(0o755)
        r = subprocess.run([str(BATON), '--watch-session', str(path), '--once'], env=self.real_env([d]),
                           cwd=str(self.home), stdin=subprocess.DEVNULL, capture_output=True, text=True,
                           timeout=TIMEOUT, start_new_session=True)
        self.assertIn('unknown', r.stdout.splitlines()[-1], r.stdout + r.stderr)

    def test_a_codex_rollout_emptied_and_refilled_is_followed_again(self):
        cwd = self.work('svc')
        tid = new_uuid()
        recs = [x_meta(tid, cwd), x_msg('user', 'first version')]
        path = self.codex_raw_rollout(tid, recs)
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('first version'))
        with open(str(path), 'w'):
            pass                                          # emptied: no session_meta for a moment
        p.pump(1.5)
        with open(str(path), 'w') as fh:
            for r in recs + [x_msg('assistant', 'after the refill')]:
                fh.write(json.dumps(r) + '\n')
        self.assertTrue(p.read_until('after the refill', secs=10), p.text())
        self.quit(p)

    def test_the_feed_shows_before_a_slow_liveness_check_finishes(self):
        cwd, sid, path = self.claude_live('shown right away')
        d = self.stub_path('python3')
        (Path(d) / 'ps').write_text('#!/bin/sh\n/bin/sleep 4\nexec /bin/ps "$@"\n')
        (Path(d) / 'ps').chmod(0o755)
        p = self.watch_pty(path, env=self.real_env([d]))
        start = time.time()
        self.assertTrue(p.read_until('shown right away', secs=3), p.text())
        self.assertLess(time.time() - start, 3)
        p.pump(5)
        self.quit(p)

    def test_an_unreadable_registry_entry_is_unknown_not_ended(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'), p.text())
        (self.home / '.claude' / 'sessions' / '4100.json').write_text('{"pid": 4100, "sessio')
        mark = len(p.buf)
        self.assertTrue(p.read_until('unknown', secs=10, start=mark), p.text(mark))
        p.pump(3)
        self.assertNotIn('session ended', p.text(mark))
        self.quit(p)

    def test_registry_removal_reports_the_session_ended(self):
        _, _, path = self.claude_live()
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'))
        (self.home / '.claude' / 'sessions' / '4100.json').unlink()
        self.assertTrue(p.read_until('session ended', secs=12), p.text())
        self.quit(p)

    def test_codex_window_that_moved_on_gets_a_notice(self):
        cwd = self.work('svc')
        tid, other = new_uuid(), new_uuid()
        path = self.codex_raw_rollout(tid, [x_meta(tid, cwd), x_msg('user', 'go'),
                                            x_event('task_started', turn_id='turn1')])
        self.proc(5100, [CODEX_BIN], cwd, open_files=[self.codex_lock(tid)])
        p = self.watch_pty(path)
        self.assertTrue(p.read_until('● busy'), p.text())
        self.procs[0]['open_files'] = [str(self.codex_lock(other))]
        self.write_procs()
        self.assertTrue(p.read_until('moved on', secs=12), p.text())
        self.quit(p)

    def test_subagents_writing_are_counted(self):
        cwd, sid, path = self.claude_live()
        fresh = self.claude_child(sid, cwd, 'a1', 'Explore', 'one')
        old = self.claude_child(sid, cwd, 'a2', 'Explore', 'two')
        os.utime(str(old), (time.time() - 300, time.time() - 300))
        _, _, status = self.feed(path)
        self.assertIn('1 subagent writing', status)
        self.assertTrue(fresh.exists())

    def test_ps_failure_is_unknown_never_ended(self):
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, [c_user(sid, cwd, 'go')], sid=sid)
        self.registry_entry(4100, sid, cwd)
        r = subprocess.run([str(BATON), '--watch-session', str(path), '--once'],
                           env=self.real_env([self.stub_path('python3')]), cwd=str(self.home),
                           stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=TIMEOUT,
                           start_new_session=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        status = r.stdout.splitlines()[-1]
        self.assertIn('unknown', status)
        self.assertNotIn('not running', status)

    def test_lsof_failure_is_unknown_for_codex(self):
        cwd = self.work('svc')
        tid = new_uuid()
        path = self.codex_raw_rollout(tid, [x_meta(tid, cwd), x_msg('user', 'go')])
        self.spawn_fake('codex')
        for dirs, want in (([self.stub_path('python3', 'ps')], 'unknown'),
                           ([self.stub_path('python3', 'ps', 'lsof')], 'not running')):
            with self.subTest(want=want):
                r = subprocess.run([str(BATON), '--watch-session', str(path), '--once'],
                                   env=self.real_env(dirs), cwd=str(self.home), stdin=subprocess.DEVNULL,
                                   capture_output=True, text=True, timeout=TIMEOUT, start_new_session=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertIn(want, r.stdout.splitlines()[-1])


class WatchReadOnlyTests(WatchLiveCase):

    def snapshot(self):
        import hashlib
        out = {}
        for dp, _, fns in os.walk(str(self.home)):
            for fn in fns:
                fp = os.path.join(dp, fn)
                st = os.lstat(fp)
                digest = ''
                if stat.S_ISREG(st.st_mode):
                    with open(fp, 'rb') as fh:
                        digest = hashlib.sha256(fh.read()).hexdigest()
                out[fp] = (st.st_size, st.st_mtime_ns, digest)
        return out

    def test_watching_never_changes_the_session_or_runs_the_tools(self):
        trap = self.home / 'trap-bin'
        trap.mkdir()
        for tool in ('claude', 'codex'):
            (trap / tool).write_text('#!/bin/sh\ntouch "$HOME/RAN-%s"\n' % tool)
            (trap / tool).chmod(0o755)
        cwd = self.work('proj')
        sid = new_uuid()
        _, path = self.claude_session(cwd, [c_user(sid, cwd, 'live and watched'),
                                            c_tool_use(sid, cwd, 'Bash', {'command': 'sleep 60'}, 'toolu_1')], sid=sid)
        self.claude_child(sid, cwd, 'a1', 'Explore', 'a child')
        import socket
        siglog = self.home / 'signals.log'
        siglog.write_text('')
        # A /bin/sh "claude" (it keeps its argv[0], unlike a framework python)
        # that logs every catchable signal it gets.
        code = ''.join("trap 'echo %s >> \"$0\"' %s; " % (n, n) for n in
                       ('TERM', 'INT', 'HUP', 'QUIT', 'USR1', 'USR2', 'WINCH', 'CONT', 'TSTP'))
        code += 'while :; do /bin/sleep 1; done'
        proc = subprocess.Popen([str(self.home / 'bin' / 'claude'), '-c', code, str(siglog)],
                                executable='/bin/sh', stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (proc.kill(), proc.wait()))
        time.sleep(0.4)
        lst = subprocess.run(['ps', '-o', 'lstart=', '-p', str(proc.pid)], capture_output=True, text=True,
                             env=dict(os.environ, TZ='UTC', LC_ALL='C')).stdout.strip()
        sock_path = str(self.home / 'cc.sock')
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        srv.bind(sock_path)
        srv.listen(1)
        srv.setblocking(False)
        self.addCleanup(srv.close)
        self.registry_entry(proc.pid, sid, cwd, procStart=lst, messagingSocketPath=sock_path)
        env = self.real_env([str(trap)] + os.environ.get('PATH', '').split(':'))
        # Apple's /usr/bin/python3 (first on CI's bash 3.2 PATH) caches stdlib
        # bytecode under $HOME/Library/Caches; that's the interpreter, not baton.
        env['PYTHONDONTWRITEBYTECODE'] = '1'
        before = self.snapshot()
        p = self.watch_pty(path, env=env)
        self.assertTrue(p.read_until('live and watched'), p.text())
        self.assertTrue(p.read_until('● busy', secs=8), p.text())
        p.pump(4.5)                                      # several polls and live checks
        self.quit(p)
        self.assertIsNone(proc.poll(), 'the watched process must keep running')
        self.assertEqual(siglog.read_text(), '', 'no signal may reach the watched process')
        with self.assertRaises(BlockingIOError, msg='nothing may connect to its messaging socket'):
            srv.accept()
        self.assertEqual(self.snapshot(), before, 'watching must not create or change any file')
        self.assertFalse((self.home / 'RAN-claude').exists() or (self.home / 'RAN-codex').exists())


class WatchPickerTests(WatchLiveCase):
    """ctrl-o from the real picker."""

    def test_ctrl_o_runs_the_watcher_on_session_and_subagent_rows_only(self):
        for path, row, runs in (('/s.jsonl', 'session', True), ('/a.jsonl', 'child', True),
                                ('/f.jsonl', 'folder', False), ('', 'session', False), ('', 'status', False)):
            with self.subTest(row=row, path=path):
                out = self.run_baton('--key', 'watch', '', path, row).stdout
                if runs:
                    self.assertTrue(out.startswith('execute('), out)
                    self.assertIn('--watch-session {1} {9})+reload(', out)
                    self.assertNotIn('track-current', out, 'tracking would block typing after the watch')
                else:
                    self.assertEqual(out, 'ignore')

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf')
    def test_ctrl_o_in_the_picker_watches_and_q_returns_to_the_list(self):
        cwd, sid, path = self.claude_live('watch me from the menu')
        p = Pty([str(BATON), '--emit'], dict(self.env(), TERM='xterm-256color'), cwd, cols=150, rows=40)
        self.addCleanup(p.close)
        self.assertTrue(p.read_until('watch me from the menu', 20), p.text()[-2000:])
        p.send(b'menu')
        self.assertTrue(p.read_until('1 match', 15), p.text()[-2000:])
        before = len(p.buf)
        p.send(b'\x0f')
        self.assertTrue(p.read_until('read-only, q stops', 15), p.text()[-2000:])
        seg = p.buf[before:]
        # fzf leaves its alternate screen first, so the feed lands in the normal
        # screen and the terminal's own scrollback keeps it.
        self.assertGreaterEqual(seg.rfind(b'\x1b[?1049l', 0, seg.find(b'watching:')), 0)
        mark = len(p.buf)
        self.append(path, c_block(sid, cwd, {'type': 'text', 'text': 'a live line from the session'}))
        self.assertTrue(p.read_until('a live line from the session', 10, start=mark), p.text(mark))
        mark = len(p.buf)
        p.send(b'q')
        self.assertTrue(p.read_until('stopped watching', 10, start=mark), p.text(mark))
        self.assertTrue(p.read_until('1 match', 10, start=mark), 'back in the list, query kept\n' + p.text(mark))
        p.send(b'\x1b')
        p.pump(0.6)
        p.send(b'\x1b')
        st = p.wait(10)
        self.assertIsNotNone(st, p.text()[-1500:])
        self.assertEqual(os.WEXITSTATUS(st), 0)


class WatchPickerMoreTests(WatchLiveCase):
    """Picker wiring around ctrl-o that depends on fzf's own state."""

    def picker(self, cwd, extra_env=None):
        env = dict(self.env(), TERM='xterm-256color', **(extra_env or {}))
        p = Pty([str(BATON), '--emit'], env, cwd, cols=150, rows=40)
        self.addCleanup(p.close)
        return p

    def watch_and_quit(self, p):
        mark = len(p.buf)
        p.send(b'\x0f')
        self.assertTrue(p.read_until('read-only, q stops', 15, start=mark), p.text(mark))
        seg = p.buf[mark:]
        mark = len(p.buf)
        p.send(b'q')
        self.assertTrue(p.read_until('stopped watching', 10, start=mark), p.text(mark))
        p.pump(1.5)                                      # the reload after watching settles
        return seg

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf')
    def test_typing_right_after_watching_keeps_every_key(self):
        cwd, sid, path = self.claude_live('watch me from the menu')
        p = self.picker(cwd)
        self.assertTrue(p.read_until('watch me from the menu', 20), p.text()[-2000:])
        self.watch_and_quit(p)
        mark = len(p.buf)
        p.send(b'from')                                  # typed fast, right after the reload
        self.assertTrue(p.read_until('1 match for "from"', 10, start=mark), p.text(mark)[-1500:])

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf')
    def test_typing_right_after_a_list_action_keeps_every_key(self):
        cwd, sid, path = self.claude_live('watch me from the menu')
        p = self.picker(cwd)
        self.assertTrue(p.read_until('watch me from the menu', 20), p.text()[-2000:])
        p.send(b'+')                                     # expand the folder: a tracked reload
        p.pump(1.5)
        mark = len(p.buf)
        p.send(b'from')
        self.assertTrue(p.read_until('1 match for "from"', 10, start=mark), p.text(mark)[-1500:])

    @unittest.skipUnless(shutil.which('fzf'), 'needs fzf')
    def test_a_height_default_in_fzf_opts_still_streams_to_the_normal_screen(self):
        cwd, sid, path = self.claude_live('height mode')
        p = self.picker(cwd, {'FZF_DEFAULT_OPTS': '--height=40%'})     # --no-popup needs tmux to test
        self.assertTrue(p.read_until('height mode', 20), p.text()[-2000:])
        seg = self.watch_and_quit(p)
        head = seg.find(b'watching:')
        self.assertGreaterEqual(seg.rfind(b'\x1b[?1049l', 0, head), 0, 'must leave the alternate screen')
        self.assertLess(seg.rfind(b'\x1b[?1049h', 0, head), seg.rfind(b'\x1b[?1049l', 0, head))


if __name__ == '__main__':
    unittest.main()
