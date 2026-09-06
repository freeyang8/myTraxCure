from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import time
from dataclasses import dataclass

from mytraxcure.core.config import CACHE_DB_PATH
from mytraxcure.core.translation.translation_engine import TranslationResult


def normalize_text(text: str) -> str:
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text

@dataclass
class CacheKey:

    source_text: str
    source_lang: str
    target_lang: str
    backend: str
    model: str
    prompt_version: str = "v1"
    term_version: str = ""
    page: int | None = None
    block_id: str | None = None

    def fingerprint(self) -> str:
        payload = {
            "text": normalize_text(self.source_text),
            "sl": self.source_lang,
            "tl": self.target_lang,
            "backend": self.backend,
            "model": self.model,
            "pv": self.prompt_version,
            "tv": self.term_version,
            "page": self.page,
            "block": self.block_id,
        }
        raw = json.dumps(payload, ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

class CacheStore:

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or CACHE_DB_PATH
        self._conn = None

    # 执行数据库连接
    def _ensure_conn(self):
        if self._conn is not None:
            return self._conn
            
        self.db_path.parent.mkdir(
            parents = True,
            exist_ok = True #目录已存在不报错
        )
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread = False #允许不同线程使用同一连接
        )
        self._conn.execute("PRAGMA journal_mode=WAL") #读写不冲突
        self._conn.execute("""
                CREATE TABLE IF NOT EXISTS translation_cache (
                    fingerprint   TEXT PRIMARY KEY,
                    source_text   TEXT NOT NULL,
                    source_lang   TEXT NOT NULL,
                    target_lang   TEXT NOT NULL,
                    backend       TEXT NOT NULL,
                    model         TEXT NOT NULL,
                    page          INTEGER,
                    block_id      TEXT,
                    translated    TEXT NOT NULL,
                    elapsed_ms    INTEGER NOT NULL,
                    file_hash     TEXT,
                    created_at    REAL NOT NULL
                )
            """)
        #在 file_hash 字段上创建索引
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_hash ON translation_cache(file_hash)"
            )
        self._conn.commit() #将上面的所有操作（建表、建索引）写入磁盘
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    #查询缓存
    def get(self, key: CacheKey) -> TranslationResult | None:
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT translated, source_text, backend, model, elapsed_ms "
            "FROM translation_cache WHERE fingerprint = ?",
            (key.fingerprint(),)
        ).fetchone() # 返回第一行结果
        if row is None:
            return None
        translated,source,backend,model,elapsed = row #解包查询结果
        translationResult = TranslationResult(
            text = translated,
            source = source,
            backend = backend,
            model = model,
            elapsed_ms= elapsed,
            cached=True
        )
        return translationResult

    def put(self, key: CacheKey, result: TranslationResult,file_hash:str|None) -> None:
        
        if result.error is not None:
            return
        conn = self._ensure_conn() #确保连接

        #插入或替换数据
        conn.execute(
            "INSERT OR REPLACE INTO translation_cache "
            "(fingerprint, source_text, source_lang, target_lang, backend, model, "
            " page, block_id, translated, elapsed_ms, file_hash, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                key.fingerprint(),
                key.source_text,
                key.source_lang,
                key.target_lang,
                key.backend,
                key.model,
                key.page,
                key.block_id,
                result.text,
                result.elapsed_ms,
                file_hash,
                time.time()
            )
        )
        conn.commit()

    # ---- 清理 ------------------------------------------------------------
    def delete_for_document(self, file_hash: str) -> None:
        raise NotImplementedError("TODO: 删除指定文档缓存")

    def cleanup_expired(self, retention_days: int = 30) -> int:
        raise NotImplementedError("TODO: 清理过期缓存")

    def cleanup_orphans(self) -> int:
        raise NotImplementedError("TODO: 清理孤儿缓存")

    def clear_all(self) -> None:
        conn = self._ensure_conn() #获取数据库连接
        conn.execute("DELETE FROM translation_cache")  #执行 SQL 删除语句
        conn.commit() #提交事务
