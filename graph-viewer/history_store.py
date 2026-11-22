"""Small local immutable analysis archive; no ledger writes or automatic eviction."""
from contextlib import contextmanager
from datetime import datetime,timezone
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading

from review_bridge import ReviewError

MAX_ENTRY_BYTES=8_000_000
MAX_TOTAL_BYTES=64_000_000
MAX_ENTRIES=100
LIST_PAGE_SIZE=20


def encoded(value):
    return json.dumps(value,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False).encode('utf-8')


def digest(raw):return hashlib.sha256(raw).hexdigest()


def history_id(value):
    if not isinstance(value,str) or not re.fullmatch(r'H-[0-9a-f]{24}',value):
        raise ReviewError(400,'invalid_history_id','저장된 분석을 목록에서 다시 선택해 주세요.')
    return value


class HistoryStore:
    def __init__(self,path,*,max_entries=MAX_ENTRIES,max_bytes=MAX_TOTAL_BYTES):
        self.path=Path(path);self.lock=threading.RLock();self.max_entries=max_entries;self.max_bytes=max_bytes

    @contextmanager
    def connection(self):
        self.path.parent.mkdir(parents=True,exist_ok=True)
        try:db=sqlite3.connect(self.path,timeout=2)
        except sqlite3.Error:
            raise ReviewError(503,'history_storage_unavailable','로컬 이력 저장소를 열지 못했습니다. 저장 위치와 사용 상태를 확인해 주세요.') from None
        db.row_factory=sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=2000');db.execute('PRAGMA synchronous=FULL')
            version=db.execute('PRAGMA user_version').fetchone()[0]
            if version not in {0,1}:raise RuntimeError('Unsupported history schema')
            tables={row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
            if version==0:
                if tables:raise ReviewError(409,'history_schema_mismatch','이 저장소는 분석 이력 전용 파일이 아닙니다. 기존 내용을 변경하지 않았습니다.')
                db.execute('''CREATE TABLE IF NOT EXISTS analysis_history (
                    id TEXT PRIMARY KEY, saved_at TEXT NOT NULL, tx_id TEXT NOT NULL,
                    has_review INTEGER NOT NULL, capture_sha256 TEXT NOT NULL UNIQUE,
                    sha256 TEXT NOT NULL, body BLOB NOT NULL, bytes INTEGER NOT NULL)''')
                db.execute('CREATE INDEX IF NOT EXISTS history_order ON analysis_history(saved_at DESC,id DESC)')
                db.execute('PRAGMA user_version=1');db.commit()
            elif tables!={'analysis_history'} or [row[1] for row in db.execute('PRAGMA table_info(analysis_history)')]!=['id','saved_at','tx_id','has_review','capture_sha256','sha256','body','bytes']:
                raise ReviewError(409,'history_schema_mismatch','저장소 구조가 분석 이력 규격과 다릅니다. 기존 내용을 변경하지 않았습니다.')
            yield db
        except sqlite3.Error:
            raise ReviewError(503,'history_storage_unavailable','로컬 이력 저장소를 확인하지 못했습니다. 기존 저장본은 덮어쓰지 않았습니다.') from None
        finally:db.close()

    @staticmethod
    def decode(row):
        if row is None:raise ReviewError(404,'history_not_found','저장된 분석을 찾지 못했습니다.')
        raw=row['body']
        try:
            if not isinstance(raw,bytes) or not 0<len(raw)<=MAX_ENTRY_BYTES or len(raw)!=row['bytes'] or not hmac.compare_digest(digest(raw),row['sha256']):
                raise ValueError('Archive digest/size mismatch')
            body=json.loads(raw)
            if (body['schemaVersion']!='1.0' or body['historyId']!=row['id'] or body['savedAt']!=row['saved_at'] or
                body['capture']['payload']['graph']['targetTxId']!=row['tx_id'] or bool(body['capture']['review'])!=bool(row['has_review']) or
                digest(encoded(body['capture']))!=row['capture_sha256']):raise ValueError('Archive identity mismatch')
            return body,row['sha256']
        except (ValueError,KeyError,TypeError):
            raise ReviewError(409,'history_integrity_failed','저장본의 내용 일치를 확인하지 못했습니다. 이 결과를 검토 근거로 사용하지 마세요.') from None

    @staticmethod
    def row(db,id):
        return db.execute('SELECT id,saved_at,tx_id,has_review,capture_sha256,sha256,substr(body,1,?) AS body,bytes FROM analysis_history WHERE id=?',(MAX_ENTRY_BYTES+1,id)).fetchone()

    def put(self,capture):
        capture_hash=digest(encoded(capture))
        with self.lock,self.connection() as db:
            db.execute('BEGIN IMMEDIATE')
            existing=db.execute('SELECT id FROM analysis_history WHERE capture_sha256=?',(capture_hash,)).fetchone()
            if existing:
                body,sha=self.decode(self.row(db,existing['id']));return body,sha,False
            body={'schemaVersion':'1.0','historyId':'H-'+secrets.token_hex(12),'savedAt':datetime.now(timezone.utc).isoformat(),'capture':capture}
            raw=encoded(body)
            if len(raw)>MAX_ENTRY_BYTES:raise ReviewError(413,'history_entry_too_large','이 분석은 한 번에 저장할 수 있는 크기를 넘었습니다.')
            count,total=db.execute('SELECT count(*),coalesce(sum(bytes),0) FROM analysis_history').fetchone()
            if count>=self.max_entries or total+len(raw)>self.max_bytes:
                raise ReviewError(409,'history_capacity','분석 이력 저장 한도에 도달했습니다. 기존 저장본을 덮어쓰지 않았습니다.')
            sha=digest(raw)
            db.execute('INSERT INTO analysis_history VALUES (?,?,?,?,?,?,?,?)',(body['historyId'],body['savedAt'],capture['payload']['graph']['targetTxId'],int(capture['review'] is not None),capture_hash,sha,raw,len(raw)))
            db.commit();return body,sha,True

    def get(self,id):
        history_id(id)
        with self.lock,self.connection() as db:return self.decode(self.row(db,id))

    def list(self,before=None):
        with self.lock,self.connection() as db:
            if before is not None:
                history_id(before);cursor=db.execute('SELECT saved_at,id FROM analysis_history WHERE id=?',(before,)).fetchone()
                if cursor is None:raise ReviewError(404,'history_not_found','목록이 바뀌었습니다. 저장 이력을 다시 조회해 주세요.')
                rows=db.execute('SELECT id FROM analysis_history WHERE (saved_at,id)<(?,?) ORDER BY saved_at DESC,id DESC LIMIT ?',(*cursor,LIST_PAGE_SIZE+1)).fetchall()
            else:rows=db.execute('SELECT id FROM analysis_history ORDER BY saved_at DESC,id DESC LIMIT ?',(LIST_PAGE_SIZE+1,)).fetchall()
            items=[self.decode(self.row(db,row['id'])) for row in rows[:LIST_PAGE_SIZE]]
            return items,rows[LIST_PAGE_SIZE-1]['id'] if len(rows)>LIST_PAGE_SIZE else None,db.execute('SELECT count(*) FROM analysis_history').fetchone()[0]
