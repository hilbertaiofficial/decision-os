"""Firestore repository for DecisionOS.

Every document keeps its full body as a JSON blob (`_doc`) plus native copies of
its top-level scalar fields. Native copies let Firestore serve equality filters;
the JSON blob preserves nested provider payloads exactly (Firestore rejects
nested arrays and some map keys). Range and `$in` filters are applied in Python
after Firestore narrows the candidates.

Uniqueness (email, active briefing per organization, active job per target,
action fingerprint, ...) is enforced with marker documents created through
Firestore's atomic `create()`, so concurrent writers cannot both succeed.
`atomic()` serializes writers in-process and records an undo log so a failed
block restores every document it touched.
"""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
import threading
import time
import uuid
from cryptography.fernet import Fernet


class DuplicateKeyError(Exception):
    """A uniqueness constraint was violated."""


class StoreUnavailable(Exception):
    """The database could not be reached or is not configured."""


def uid(): return uuid.uuid4().hex

def defaults():
    return {'mode':'demo','engine':'gemini','schedule_enabled':False,'interval_minutes':60,'next_run':0,
            'auto_drafts':True,'auto_mark_read':False,'jira_jql':'assignee = currentUser() AND resolution = Unresolved ORDER BY updated DESC'}


# ---------------------------------------------------------------- query helpers

def _get(doc, path):
    for part in path.split('.'):
        if not isinstance(doc, dict) or part not in doc: return None
        doc = doc[part]
    return doc

def _set(doc, path, value):
    parts = path.split('.')
    for part in parts[:-1]: doc = doc.setdefault(part, {})
    doc[parts[-1]] = value

def _compare(value, op, expected):
    if op == '$in': return value in expected
    if op == '$nin': return value not in expected
    if op == '$ne': return value != expected
    if value is None: return False
    if op == '$gt': return value > expected
    if op == '$gte': return value >= expected
    if op == '$lt': return value < expected
    if op == '$lte': return value <= expected
    raise ValueError('Unsupported query operator: ' + op)

def matches(doc, query):
    for key, expected in (query or {}).items():
        if key == '$or':
            if not any(matches(doc, sub) for sub in expected): return False
            continue
        value = _get(doc, key)
        if isinstance(expected, dict) and expected and all(k.startswith('$') for k in expected):
            if not all(_compare(value, op, arg) for op, arg in expected.items()): return False
        elif value != expected: return False
    return True

def _sort(rows, sort):
    for key, direction in reversed(sort or []):
        rows.sort(key=lambda r: (_get(r, key) is not None, _get(r, key) if _get(r, key) is not None else 0), reverse=direction < 0)
    return rows

def _scalar(value):
    return value is None or isinstance(value, (str, bool, int, float, datetime))

def _pushdown(query):
    """Equality filters Firestore can evaluate natively on stored scalar copies."""
    return {k: v for k, v in (query or {}).items() if not k.startswith('$') and '.' not in k and _scalar(v)}


class _Encoder(json.JSONEncoder):
    def default(self, value):
        if isinstance(value, datetime): return {'$dt': value.astimezone(timezone.utc).isoformat()}
        return super().default(value)

def _hook(obj):
    if len(obj) == 1 and '$dt' in obj: return datetime.fromisoformat(obj['$dt'])
    return obj

def encode(doc):
    stored = {k: v for k, v in doc.items() if _scalar(v)}
    stored['_doc'] = json.dumps(doc, cls=_Encoder)
    return stored

def decode(stored):
    return json.loads(stored['_doc'], object_hook=_hook) if stored else None


# Unique constraints: (collection) -> list of functions returning a key or None.
UNIQUE = {
    'users': [lambda d: 'email:' + d['email'] if d.get('email') else None],
    'memberships': [lambda d: f"member:{d.get('org_id')}:{d.get('user_id')}"],
    'connections': [lambda d: f"conn:{d.get('org_id')}:{d.get('provider')}:{d.get('mode')}:{d.get('identity')}" if d.get('identity') else None],
    'items': [lambda d: f"item:{d.get('org_id')}:{d.get('connection_id')}:{d.get('kind')}:{d.get('external_id')}"],
    'actions': [lambda d: 'fp:' + d['fingerprint'] if d.get('fingerprint') else None],
    'runs': [lambda d: 'run-active:' + str(d.get('org_id')) if d.get('active') else None],
    'jobs': [lambda d: f"job-active:{d.get('kind')}:{d.get('target')}" if d.get('active') else None],
}


# ---------------------------------------------------------------- backends

class FirestoreBackend:
    """Thin adapter over google-cloud-firestore."""
    def __init__(self, client, prefix=''):
        from google.cloud import firestore
        self.client, self.prefix, self.firestore = client, prefix, firestore

    def ref(self, collection, doc_id): return self.client.collection(self.prefix + collection).document(doc_id)
    def get(self, collection, doc_id):
        snap = self.ref(collection, doc_id).get()
        return snap.to_dict() if snap.exists else None
    def set(self, collection, doc_id, data): self.ref(collection, doc_id).set(data)
    def delete(self, collection, doc_id): self.ref(collection, doc_id).delete()
    def create(self, collection, doc_id, data):
        from google.api_core.exceptions import AlreadyExists
        try: self.ref(collection, doc_id).create(data)
        except AlreadyExists: raise DuplicateKeyError(doc_id) from None
    def query(self, collection, equals):
        from google.cloud.firestore_v1.base_query import FieldFilter
        q = self.client.collection(self.prefix + collection)
        for key, value in equals.items(): q = q.where(filter=FieldFilter(key, '==', value))
        return [s.to_dict() for s in q.stream()]
    def transact(self, collection, doc_id, fn):
        """Read-modify-write one document atomically. fn(current) -> (new_or_None, result)."""
        ref = self.ref(collection, doc_id)
        @self.firestore.transactional
        def run(transaction):
            snap = ref.get(transaction=transaction)
            new, result = fn(snap.to_dict() if snap.exists else None)
            if new is not None: transaction.set(ref, new)
            return result
        return run(self.client.transaction())
    def ping(self): list(self.client.collection(self.prefix + 'leases').limit(1).stream())


class MemoryBackend:
    """In-process backend with Firestore semantics, used by tests and local demos."""
    def __init__(self): self.data, self.lock = {}, threading.RLock()
    def get(self, collection, doc_id):
        with self.lock: return deepcopy(self.data.get(collection, {}).get(doc_id))
    def set(self, collection, doc_id, data):
        with self.lock: self.data.setdefault(collection, {})[doc_id] = deepcopy(data)
    def delete(self, collection, doc_id):
        with self.lock: self.data.get(collection, {}).pop(doc_id, None)
    def create(self, collection, doc_id, data):
        with self.lock:
            if doc_id in self.data.get(collection, {}): raise DuplicateKeyError(doc_id)
            self.set(collection, doc_id, data)
    def query(self, collection, equals):
        with self.lock:
            return [deepcopy(d) for d in self.data.get(collection, {}).values() if all(d.get(k) == v for k, v in equals.items())]
    def transact(self, collection, doc_id, fn):
        with self.lock:
            new, result = fn(self.get(collection, doc_id))
            if new is not None: self.set(collection, doc_id, new)
            return result
    def ping(self): pass


# ---------------------------------------------------------------- store

class Store:
    def __init__(self, backend, encryption_key, testing=False):
        self.backend, self.testing = backend, testing
        self.cipher = Fernet(encryption_key)
        self._undo = ContextVar('undo_log', default=None)
        self._lock = threading.RLock()
        self.execution_owner = ContextVar('execution_owner', default=None)

    @classmethod
    def from_env(cls):
        key = os.getenv('COS_ENCRYPTION_KEY')
        project = os.getenv('GOOGLE_CLOUD_PROJECT') or os.getenv('FIRESTORE_PROJECT')
        if os.getenv('DECISIONOS_STORE') == 'memory':
            if not key: raise StoreUnavailable('Set COS_ENCRYPTION_KEY before starting the application.')
            return cls(MemoryBackend(), key)
        if not project or not key:
            raise StoreUnavailable('Set GOOGLE_CLOUD_PROJECT and COS_ENCRYPTION_KEY before starting the application.')
        try:
            from google.cloud import firestore
            client = firestore.Client(project=project, database=os.getenv('FIRESTORE_DATABASE', '(default)'))
        except Exception as exc:
            raise StoreUnavailable('Firestore is unavailable: ' + exc.__class__.__name__) from None
        return cls(FirestoreBackend(client, os.getenv('FIRESTORE_COLLECTION_PREFIX', '')), key)

    def ping(self): self.backend.ping()

    # ---- undo log / atomic blocks
    def _record(self, collection, doc_id, previous):
        log = self._undo.get()
        if log is not None: log.append((collection, doc_id, previous))

    @contextmanager
    def atomic(self):
        if self._undo.get() is not None:
            yield; return
        with self._lock:
            log = []
            token = self._undo.set(log)
            try: yield
            except Exception:
                for collection, doc_id, previous in reversed(log):
                    if previous is None: self.backend.delete(collection, doc_id)
                    else: self.backend.set(collection, doc_id, previous)
                raise
            finally: self._undo.reset(token)

    # ---- uniqueness markers
    def _keys(self, collection, doc):
        return {k for k in (fn(doc) for fn in UNIQUE.get(collection, [])) if k}

    def _marker(self, key): return hashlib.sha256(key.encode()).hexdigest()

    def _claim_keys(self, collection, keys, doc_id):
        created = []
        try:
            for key in keys:
                marker = self._marker(collection + ':' + key)
                self.backend.create('_unique', marker, {'owner': doc_id, 'collection': collection})
                created.append(marker); self._record('_unique', marker, None)
        except DuplicateKeyError:
            for marker in created: self.backend.delete('_unique', marker)
            raise

    def _release_keys(self, collection, keys):
        for key in keys:
            marker = self._marker(collection + ':' + key)
            previous = self.backend.get('_unique', marker)
            if previous is not None:
                self._record('_unique', marker, previous); self.backend.delete('_unique', marker)

    # ---- generic document API (Mongo-style query dicts)
    def find(self, collection, query=None, sort=None, limit=0):
        query = query or {}
        if isinstance(query.get('id'), str):
            stored = self.backend.get(collection, query['id'])
            rows = [decode(stored)] if stored else []
        else:
            rows = [decode(s) for s in self.backend.query(collection, _pushdown(query))]
        rows = _sort([r for r in rows if matches(r, query)], sort)
        return rows[:limit] if limit else rows

    def one(self, collection, query):
        rows = self.find(collection, query, limit=1)
        return rows[0] if rows else None

    def insert(self, collection, document):
        doc = deepcopy(document); doc.setdefault('id', uid())
        with self._lock:
            if self.backend.get(collection, doc['id']) is not None: raise DuplicateKeyError(doc['id'])
            self._claim_keys(collection, self._keys(collection, doc), doc['id'])
            self.backend.set(collection, doc['id'], encode(doc)); self._record(collection, doc['id'], None)
        return doc

    def _write(self, collection, old, new):
        before, after = self._keys(collection, old), self._keys(collection, new)
        self._claim_keys(collection, after - before, new['id'])
        self._release_keys(collection, before - after)
        self._record(collection, new['id'], encode(old))
        self.backend.set(collection, new['id'], encode(new))

    def update(self, collection, query, values, many=False):
        with self._lock:
            rows = self.find(collection, query, limit=0 if many else 1)
            for row in rows:
                new = deepcopy(row)
                for path, value in values.items(): _set(new, path, value)
                self._write(collection, row, new)
            return len(rows)

    def delete(self, collection, query):
        with self._lock:
            rows = self.find(collection, query)
            for row in rows:
                self._release_keys(collection, self._keys(collection, row))
                self._record(collection, row['id'], encode(row))
                self.backend.delete(collection, row['id'])
            return len(rows)

    def claim(self, collection, query, values, sort=None):
        """Atomically move the first matching document to a new state."""
        for candidate in self.find(collection, query, sort=sort):
            def change(current):
                doc = decode(current)
                if not doc or not matches(doc, query): return None, None
                new = deepcopy(doc)
                for path, value in values.items(): _set(new, path, value)
                return encode(new), (doc, new)
            with self._lock:
                outcome = self.backend.transact(collection, candidate['id'], change)
                if outcome:
                    old, new = outcome
                    before, after = self._keys(collection, old), self._keys(collection, new)
                    self._release_keys(collection, before - after)
                    self._claim_keys(collection, after - before, new['id'])
                    return new
        return None

    # ---- domain helpers
    def settings(self, org_id=None):
        if not org_id: return defaults()
        org=self.one('organizations',{'id':org_id,'status':'active'})
        if not org: raise ValueError('Organization not available.')
        return dict(defaults(),**org['config'])

    def setting(self,key,value,org_id):
        self.update('organizations',{'id':org_id,'status':'active'},{'config.'+key:value})

    def event(self,message,level='info',org_id=None,actor=None):
        self.insert('events',{'message':message,'level':level,'org_id':org_id,'actor':actor,'created':time.time()})

    def membership(self,org_id,user_id):
        return self.one('memberships',{'org_id':org_id,'user_id':user_id})

    def organizations_for(self,user_id):
        ids=[m['org_id'] for m in self.find('memberships',{'user_id':user_id})]
        return self.find('organizations',{'id':{'$in':ids},'status':'active'},sort=[('created',1)])

    def create_organization(self,user_id,name,mode='live'):
        from . import billing
        if len(self.organizations_for(user_id))>=20: raise ValueError('Organization limit reached. Contact support.')
        owned_ids=[m['org_id'] for m in self.find('memberships',{'user_id':user_id,'role':'owner'})]
        owned=self.find('organizations',{'id':{'$in':owned_ids},'status':'active'})
        limit=billing.workspace_limit(o.get('plan','trial') for o in owned)
        if len(owned)>=limit:
            raise ValueError(f'Workspace limit reached ({limit}). Upgrade a workspace to Pro or Team to raise this limit.')
        with self.atomic():
            org=self.insert('organizations',{'name':name,'status':'active','config':dict(defaults(),mode=mode),'created':time.time(),'auth_revision':0,
                'plan':'trial','trial_briefings_used':0,'subscription_id':None,'subscription_status':None})
            self.insert('memberships',{'org_id':org['id'],'user_id':user_id,'role':'owner','created':time.time()})
        return org

    def connection(self,cid,secret=False,org_id=None):
        query={'id':cid}
        if org_id: query['org_id']=org_id
        conn=self.one('connections',query)
        if conn:
            encrypted=conn.pop('secrets')
            if secret: conn['secrets']=json.loads(self.cipher.decrypt(encrypted.encode()))
        return conn

    def connections(self,mode=None,org_id=None):
        query={}
        if mode: query['mode']=mode
        if org_id: query['org_id']=org_id
        return [self.connection(c['id']) for c in self.find('connections',query,sort=[('created',1)])]

    def connect(self,provider,label,tokens,metadata,mode='live',org_id=None,owner_id=None):
        if not org_id: raise ValueError('An organization is required.')
        self.settings(org_id)
        query={'org_id':org_id,'provider':provider,'mode':mode,'identity':metadata['identity']}
        values={'label':label,'metadata':metadata,'secrets':self.cipher.encrypt(json.dumps(tokens).encode()).decode(),
                'status':'connected','error':None,'grant_owner':owner_id}
        with self.atomic():
            existing=self.one('connections',query)
            if existing:
                cid=existing['id']; self.update('connections',{'id':cid},values)
            else:
                cid=self.insert('connections',dict(query,**values,created=time.time(),last_sync=None))['id']
            if mode=='live': self.save_tokens(cid,tokens)
        return cid

    def save_tokens(self,cid,tokens):
        conn=self.connection(cid)
        # Never share OAuth grants between different application users merely because provider metadata matches.
        query={'provider':conn['provider'],'identity':conn['identity'],'grant_owner':conn.get('grant_owner'),'status':'connected','mode':'live'}
        if not conn.get('grant_owner'): query['id']=cid
        self.update('connections',query,{'secrets':self.cipher.encrypt(json.dumps(tokens).encode()).decode()},many=True)

    def disconnect(self,cid):
        with self.atomic():
            conn=self.connection(cid)
            if self.one('actions',{'connection_id':cid,'status':'executing'}): raise ValueError('Wait for the current action to finish.')
            self.update('connections',{'id':cid},{'status':'disconnected','secrets':self.cipher.encrypt(b'{}').decode()})
            self.update('actions',{'connection_id':cid,'status':{'$in':['proposed','queued','failed']}},{'status':'dismissed','updated':time.time()},many=True)
            self.event('Account disconnected; pending actions canceled.',org_id=conn['org_id'])

    def enqueue(self,kind,target,org_id=None):
        try:
            self.insert('jobs',{'kind':kind,'target':target,'org_id':org_id,'status':'queued','active':True,'created':time.time()})
            return True
        except DuplicateKeyError: return False

    def claim_job(self,owner):
        return self.claim('jobs',{'status':'queued','active':True},{'status':'running','owner':owner,'started':time.time()},sort=[('created',1)])

    def lease(self,owner):
        now=time.time()
        def change(current):
            doc=decode(current)
            if doc and doc['owner']!=owner and doc['expires']>=now: return None, False
            return encode({'id':'worker','owner':owner,'expires':now+45}), True
        return self.backend.transact('leases','worker',change)

    def owns_lease(self,owner):
        return bool(self.one('leases',{'id':'worker','owner':owner,'expires':{'$gt':time.time()}}))

    def rate_limit(self,key,limit,seconds):
        bucket=int(time.time()//seconds)
        rid=hashlib.sha256(f'{key}:{bucket}'.encode()).hexdigest()
        def change(current):
            doc=decode(current) or {'id':rid,'count':0,'expires_at':datetime.fromtimestamp((bucket+2)*seconds,timezone.utc)}
            doc['count']+=1
            return encode(doc), doc['count']
        return self.backend.transact('rate_limits',rid,change)<=limit

    def check_execution(self):
        owner=self.execution_owner.get()
        if owner and not self.owns_lease(owner):raise ValueError('Worker lease lost. No further provider calls may execute.')
