"""Atomic workspace snapshots with a DATA_DIR-wide storage budget.

All writers, deletion and sweeping take the same process and file lock. SQLite
changes happen in memory; the final serialized snapshot is reserved before any
bytes are written. The reservation includes the old snapshot, temporary file,
rate-limit file and all other files in DATA_DIR. No on-disk SQLite journal or WAL
can grow outside the budget. Deployment requires one private DATA_DIR on a local
filesystem supporting flock and atomic rename.
"""
import contextlib
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import threading
import time
import fcntl
from rebate_rail.intent_log import Entry, IntentLog
from rebate_rail.plan import DEFAULT_RULE, build_run, downloadable, validate_rule

MAX_WORKSPACE = 200 * 1024 * 1024
RETENTION_SECONDS = 90 * 86400
_ROOT_LOCKS, _ROOT_LOCKS_GUARD = {}, threading.Lock()


class RateLimited(Exception):
    pass


class Store:
    def __init__(self, root, cap_mb=200, clock=time.time):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cap = int(cap_mb) * 1024 * 1024
        if self.cap <= 0:
            raise ValueError('APP_DATA_CAP_MB must be positive')
        self.clock, self.locks = clock, {}
        with _ROOT_LOCKS_GUARD:
            self._lock = _ROOT_LOCKS.setdefault(str(self.root), threading.RLock())
        self._lock_path = self.root / '.store.lock'
        self._lock_path.touch(mode=0o600, exist_ok=True)
        self.sweep()

    @contextlib.contextmanager
    def _guard(self):
        with self._lock, self._lock_path.open('rb') as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def total_size(path):
        total = 0
        with os.scandir(path) as entries:
            for entry in entries:
                if entry.is_symlink():
                    raise RuntimeError('DATA_DIR must not contain symlinks')
                if entry.is_dir(follow_symlinks=False):
                    total += Store.total_size(entry.path)
                else:
                    total += entry.stat(follow_symlinks=False).st_size
        return total

    def _size(self):
        # Keep the public accounting hook at one call per transaction.
        return self.total_size(self.root)

    @staticmethod
    def _digest(token):
        if not isinstance(token, str) or not re.fullmatch(r'[A-Za-z0-9_-]{32,128}', token):
            raise KeyError('workspace')
        return hashlib.sha256(token.encode()).hexdigest()

    def resolve(self, token):
        digest = self._digest(token)
        path = self.root / digest
        if not path.is_dir() or path.is_symlink():
            raise KeyError('workspace')
        return digest, path

    def _touch(self, path):
        os.utime(path, (self.clock(), self.clock()))

    @staticmethod
    def _schema(db):
        db.executescript('CREATE TABLE IF NOT EXISTS documents (name TEXT PRIMARY KEY, value TEXT NOT NULL); CREATE TABLE IF NOT EXISTS audit (seq INTEGER PRIMARY KEY, value TEXT NOT NULL);')

    @staticmethod
    def _put(db, name, value):
        db.execute('INSERT OR REPLACE INTO documents VALUES (?, ?)', (name, json.dumps(value, separators=(',', ':'), allow_nan=False)))

    @staticmethod
    def _get(db, name):
        row = db.execute('SELECT value FROM documents WHERE name=?', (name,)).fetchone()
        if row is None:
            raise KeyError(name)
        return json.loads(row[0])

    def _load(self, path):
        db = sqlite3.connect(':memory:')
        try:
            filename = path / 'workspace.sqlite3'
            if filename.exists():
                # SQLite performs any required rollback recovery before backup.
                with sqlite3.connect(filename) as source:
                    source.backup(db)
            else:
                self._schema(db)
                self._put(db, 'meta.json', json.loads((path / 'meta.json').read_text()))
                self._put(db, 'rule.json', json.loads((path / 'rule.json').read_text()))
                for filename in sorted((path / 'runs').glob('*.json')):
                    self._put(db, 'runs/' + filename.name, json.loads(filename.read_text()))
                for entry in IntentLog(path / 'log.jsonl').entries():
                    db.execute('INSERT INTO audit VALUES (?,?)', (entry.seq, json.dumps(entry.__dict__)))
                db.commit()
            return db
        except Exception:
            db.close()
            raise

    def _atomic(self, target, content, used):
        if used + len(content) > self.cap:
            raise RuntimeError('Shared storage full: export or delete a workspace')
        temporary = target.parent / ('.pending-' + secrets.token_hex(12))
        try:
            with temporary.open('xb') as out:
                os.chmod(temporary, 0o600)
                out.write(content)
                out.flush()
                os.fsync(out.fileno())
            old_size = target.stat().st_size if target.exists() else 0
            os.replace(temporary, target)
            descriptor = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return used - old_size + len(content)
        finally:
            temporary.unlink(missing_ok=True)

    def _save(self, path, db, used=None):
        db.commit()
        content = db.serialize()
        if len(content) > MAX_WORKSPACE:
            raise RuntimeError('Workspace storage full: export or delete')
        self._atomic(path / 'workspace.sqlite3', content, self._size() if used is None else used)
        self._touch(path)

    def _read(self, token, action):
        with self._guard():
            _, path = self.resolve(token)
            db = self._load(path)
            try:
                result = action(db)
                self._touch(path)
                return result
            finally:
                db.close()

    def _write(self, token, action):
        with self._guard():
            _, path = self.resolve(token)
            db = self._load(path)
            try:
                result = action(db)
                meta = self._get(db, 'meta.json')
                meta.update(updated_at=self.clock(), written=True)
                self._put(db, 'meta.json', meta)
                self._save(path, db)
                return result
            finally:
                db.close()

    def create(self, sample=False, actor='local', actor_limit=60, global_limit=None):
        with self._guard():
            now = self.clock()
            budget_path = self.root / '.creation-budget.json'
            budget = json.loads(budget_path.read_text()) if budget_path.exists() else []
            budget = [event for event in budget if now - event[0] < 3600]
            actor_hash = hashlib.sha256(str(actor).encode()).hexdigest()
            if sum(event[1] == actor_hash for event in budget) >= actor_limit:
                raise RateLimited('Workspace creation limit for this client; retry in an hour')
            if len(budget) >= (global_limit if global_limit is not None else 4096):
                raise RateLimited('Workspace creation limit; retry in an hour')
            token = secrets.token_urlsafe(32)
            path = self.root / self._digest(token)
            if path.exists():
                raise RuntimeError('Token collision; retry')
            db = sqlite3.connect(':memory:')
            try:
                self._schema(db)
                self._put(db, 'meta.json', {'created_at': now, 'updated_at': now, 'sample': bool(sample), 'written': False})
                self._put(db, 'rule.json', DEFAULT_RULE)
                db.commit()
                content = db.serialize()
                encoded = json.dumps(budget + [[now, actor_hash]], separators=(',', ':')).encode()
                used = self._size()
                if used + len(content) + len(encoded) > self.cap:
                    raise RuntimeError('Shared storage full: export or delete a workspace')
                used = self._atomic(budget_path, encoded, used)
                path.mkdir(mode=0o700)
                try:
                    self._atomic(path / 'workspace.sqlite3', content, used)
                    self._touch(path)
                except Exception:
                    shutil.rmtree(path)
                    raise
                return token
            finally:
                db.close()

    def meta(self, token):
        return self._read(token, lambda db: self._get(db, 'meta.json'))

    def rule(self, token):
        return self._read(token, lambda db: self._get(db, 'rule.json'))

    @staticmethod
    def _runs(db):
        return [json.loads(row[0]) for row in db.execute("SELECT value FROM documents WHERE name LIKE 'runs/%' ORDER BY rowid")]

    def runs(self, token):
        return self._read(token, self._runs)

    def run(self, token, run_id):
        return self._read(token, lambda db: self._get(db, 'runs/' + run_id + '.json'))

    def write(self, token, name, value):
        if name != 'rule.json':
            raise ValueError('unsupported document')
        value = validate_rule(value)
        return self._write(token, lambda db: self._put(db, name, value))

    def _append(self, db, records):
        row = db.execute('SELECT seq,value FROM audit ORDER BY seq DESC LIMIT 1').fetchone()
        seq, previous = (0, '0' * 64) if row is None else (row[0], json.loads(row[1])['hash'])
        for event, reference, data in records:
            seq += 1
            payload = {'seq': seq, 'ts': int(self.clock()), 'event_type': event, 'intent_id': reference, 'data': data}
            digest = IntentLog._digest(previous, payload)
            value = dict(payload, prev_hash=previous, hash=digest)
            db.execute('INSERT INTO audit VALUES (?,?)', (seq, json.dumps(value, separators=(',', ':'))))
            previous = digest

    def append_log(self, token, event, reference, data):
        return self._write(token, lambda db: self._append(db, [(event, reference, data)]))

    def create_run(self, token, rows, dropped):
        def change(db):
            if db.execute("SELECT count(*) FROM documents WHERE name LIKE 'runs/%'").fetchone()[0] >= 24:
                raise ValueError('at most 24 runs per workspace')
            run = build_run(rows, self._get(db, 'rule.json'), dropped, self.clock())
            self._append(db, [(item['state'], item['reference'], {'account_id': item['account_id'], 'amount_cents': item['amount_cents'], 'reasons': item['reasons']}) for item in run['intents'] if item['reference']])
            self._put(db, 'runs/' + run['id'] + '.json', run)
            return run
        return self._write(token, change)

    def mutate_run(self, token, run_id, action):
        def change(db):
            name = 'runs/' + run_id + '.json'
            run = self._get(db, name)
            records = action(run, db)
            self._append(db, records or [])
            self._put(db, name, run)
            return run
        return self._write(token, change)

    def known_references(self, db):
        return {item['reference'] for run in self._runs(db) for item in run['intents'] if item['reference']}

    def downloads(self, token, run_id):
        def read(db):
            runs = self._runs(db)
            run = next((value for value in runs if value['id'] == run_id), None)
            if run is None:
                raise KeyError(run_id)
            return downloadable(run, runs)
        return self._read(token, read)

    def export(self, token):
        def read(db):
            entries = [json.loads(row[0]) for row in db.execute('SELECT value FROM audit ORDER BY seq')]
            log = IntentLog()
            log._entries = [Entry(**entry) for entry in entries]
            if not log.verify():
                raise ValueError('hash chain verification failed')
            return {'meta': self._get(db, 'meta.json'), 'rule': self._get(db, 'rule.json'), 'runs': self._runs(db), 'log': entries}
        return self._read(token, read)

    def rotate_share(self, token):
        share = secrets.token_urlsafe(32)
        def change(db):
            meta = self._get(db, 'meta.json')
            meta['share_hash'] = self._digest(share)
            self._put(db, 'meta.json', meta)
        self._write(token, change)
        return share

    def shared(self, token):
        digest = self._digest(token)
        with self._guard():
            for path in self.root.iterdir():
                if not path.is_dir() or not re.fullmatch('[a-f0-9]{64}', path.name):
                    continue
                db = self._load(path)
                try:
                    meta = self._get(db, 'meta.json')
                    if meta.get('share_hash') == digest:
                        self._touch(path)
                        return {'sample': meta.get('sample', False), 'rule': self._get(db, 'rule.json'), 'runs': self._runs(db)}
                finally:
                    db.close()
        raise KeyError('share')

    def delete(self, token):
        with self._guard():
            digest, path = self.resolve(token)
            # All readers and writers resolve again under this lock. Removed
            # workspaces cannot be resurrected by a previously queued writer.
            shutil.rmtree(path)
            self.locks.pop(digest, None)

    def sweep(self):
        with self._guard():
            now = self.clock()
            for path in list(self.root.iterdir()):
                if path.name.startswith('.pending-'):
                    path.unlink(missing_ok=True)
                elif path.is_dir() and re.fullmatch('[a-f0-9]{64}', path.name):
                    for temporary in path.glob('.pending-*'):
                        temporary.unlink(missing_ok=True)
                    if now - path.stat().st_mtime >= RETENTION_SECONDS:
                        shutil.rmtree(path)
                        self.locks.pop(path.name, None)
