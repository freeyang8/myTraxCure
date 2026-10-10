"""模块名称：cache_store.py

功能描述：译文缓存的持久化层。把一次翻译的结果按内容指纹存进 SQLite
（data/cache.db），即时翻译负责读命中，后台预翻译负责写，
是"预翻译换即时出译文"这条链路的存储落点。

关键约束：缓存键只能由 make_segment_key 构造——预翻译与即时翻译必须算出同一个
fingerprint，否则预翻译写进去的结果永远命不中；指纹不含上下文与术语表字段，
所以这两项一旦真的参与翻译，同一段的两版译文会撞同一个键互相覆盖。
"""

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
    """抹平排版差异：去首尾空白、把连续空白折叠成单个空格。

    只用于算指纹，不改动真正送去翻译或存进库的原文。
    """
    text = text.strip()
    text = re.sub(r"\s+", " ", text)
    return text

@dataclass
class CacheKey:
    """一条缓存的身份标识：源文本 + 语言对 + 后端/模型 + 版本 + 版面位置。

    版面位置（page / block_id）也进指纹：重排后的同一句话落在不同位置时算两条。
    """
    source_text: str
    source_lang: str
    target_lang: str
    backend: str  # 换后端即换一批缓存：不同后端的译文风格不可混用
    model: str  # 换模型同样要失效：小模型与大模型的译文质量差很多
    # 提示词模板改版后 bump 此值即可让旧译文整体失效（当前未接线，恒为 "v1"）
    prompt_version: str = "v1"
    # 术语表版本：术语表尚未接入，恒为空串，但仍参与指纹
    term_version: str = ""
    page: int | None = None
    block_id: str | None = None  # 段落首块 id，用来区分文本相同的不同段落

    def fingerprint(self) -> str:
        """把参与身份的字段序列化后取 SHA-256，即缓存表的主键。

        Returns:
            str: 64 位十六进制摘要。

        Notes:
            sort_keys=True 让字段顺序与 Python 版本无关，避免升级后指纹漂移；
            source_text 先过 normalize_text，重排视图里换行不同的同一段仍能命中。
        """
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

def make_segment_key(
    segment: tuple[str, str, int, str], cfg, backend: str
) -> CacheKey:
    """段落元组 (para_key, 文本, page, 首块 block_id) → 缓存键。

    预翻译与即时翻译必须共用这一个构造函数：任何一处字段不一致，
    预翻译写进去的结果就永远命不中，整个预翻译等于白做。

    Args:
        segment: text_mapper 产出的段落四元组；para_key 只用于取值辨识，不进指纹。
        cfg: TranslationConfig，取 source_lang / target_lang / model。
        backend: 引擎名（当前为 "ollama"），随引擎实例一起决定缓存归属。

    Returns:
        CacheKey: 可直接交给 CacheStore.get / put 的键。

    Notes:
        文本与首块 id 已经足以区分段落，所以 para_key 被丢弃是有意的——
        重排后 para_key 可能变，带上它会让同一段内容反复生成新缓存。
    """
    _para_key, text, page, block_id = segment
    return CacheKey(
        source_text=text,
        source_lang=cfg.source_lang,
        target_lang=cfg.target_lang,
        backend=backend,
        model=cfg.model,
        page=page,
        block_id=block_id,
    )


class CacheStore:
    """SQLite 译文缓存：连接惰性建立、进程内长期持有，close() 后可再自动重连。

    并发：建连接时关掉了 sqlite 的线程归属检查（check_same_thread=False），
    并用 WAL 日志，使主线程（即时翻译读写）与预翻译线程（各自一条连接）
    能同时访问同一个库文件而读写互不阻塞。
    """

    def __init__(self, db_path=None) -> None:
        self.db_path = db_path or CACHE_DB_PATH
        self._conn = None

    def _ensure_conn(self):
        """返回可用连接；首次调用时建目录、连库、建表建索引，且整个流程幂等。

        惰性建连接：构造 CacheStore 不该碰磁盘，避免启动期就锁住库文件。
        """
        if self._conn is not None:
            return self._conn

        self.db_path.parent.mkdir(
            parents = True,
            exist_ok = True #每次启动都会走到这里，目录已存在时不能报错
        )
        self._conn = sqlite3.connect(
            self.db_path,
            check_same_thread = False #关闭线程归属检查：实例可能在非创建线程里被复用
        )
        self._conn.execute("PRAGMA journal_mode=WAL") #WAL：读命中与预翻译写盘不互相阻塞
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
        #file_hash 索引为"按文档批量清理"预留；当前所有写入都传 file_hash=None
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_file_hash ON translation_cache(file_hash)"
            )
        self._conn.commit() #DDL 也需显式提交，否则连接断开后表结构不落盘
        return self._conn

    def close(self) -> None:
        """断开本实例的连接；置空后可被下一次 get/put 重新建起。"""
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def get(self, key: CacheKey) -> TranslationResult | None:
        """按指纹查一条缓存。

        Args:
            key: make_segment_key 产出的键。

        Returns:
            TranslationResult | None: 命中则 cached=True 的结果；未命中返回 None。

        Notes:
            命中结果不带 ttft_ms：首字延迟无法复现，UI 走"缓存"分支展示耗时即可。
        """
        conn = self._ensure_conn()
        row = conn.execute(
            "SELECT translated, source_text, backend, model, elapsed_ms "
            "FROM translation_cache WHERE fingerprint = ?",
            (key.fingerprint(),)
        ).fetchone() #主键等值查询，最多命中一行
        if row is None:
            return None
        translated,source,backend,model,elapsed = row
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
        """写入一条缓存；失败结果不入库，成功结果覆盖同键旧值。

        Args:
            key: make_segment_key 产出的键。
            result: 翻译结果；error 非空时直接返回，不写库。
            file_hash: 源文档哈希，供按文档清理用；当前调用方一律传 None。

        Notes:
            失败结果若入库，下一次请求会命中一条空译文，等于把一次抖动固化成
            永久故障，所以这里必须拦掉。
        """

        if result.error is not None:
            return
        conn = self._ensure_conn()

        #覆盖写：换模型/改术语表后重新翻译，以新译文为准，不留历史版本
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
    def clear_all(self) -> None:
        """清空整张缓存表（设置页"清空缓存"用）；表结构与索引保留。

        这是当前唯一的回收手段：缓存既没有过期时间也没有条数上限，
        长期不清理 data/cache.db 会一直增长。
        """
        conn = self._ensure_conn()
        conn.execute("DELETE FROM translation_cache")
        conn.commit()
