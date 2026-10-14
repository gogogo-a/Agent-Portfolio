#!/usr/bin/env python3
"""滑动窗口语义分块（方案 B）：按句切 → 双窗 embedding 比相似度 → 变化大处打 ##\\n。"""

from __future__ import annotations

import argparse
import csv
import json
import random
import re
import statistics
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import requests
import yaml

_HERE = Path(__file__).resolve().parent
# 脚本在 Conf 同级的 scripts/，或与 config_server.yaml 同目录（麒麟离线包）
if (_HERE / "config_server.yaml").exists():
    DEFAULT_CFG = _HERE / "config_server.yaml"
elif (_HERE.parent / "Conf" / "semantic_chunk.yaml").exists():
    DEFAULT_CFG = _HERE.parent / "Conf" / "semantic_chunk.yaml"
else:
    DEFAULT_CFG = _HERE / "semantic_chunk.yaml"

SENT_SPLIT = re.compile(r"(?<=[。！？；\n])")
CLAUSE_HEAD = re.compile(r"^第[一二三四五六七八九十百千零〇\d]+条")
CHAPTER_HEAD = re.compile(r"^第[一二三四五六七八九十百千零〇\d]+章")
ARTICLE_HEAD = re.compile(r"第[一二三四五六七八九十百千零〇\d]+条")
ENUM_CN_HEAD = re.compile(r"^（[一二三四五六七八九十百千零〇]+）")
ENUM_NUM_HEAD = re.compile(r"^\d+[．.]")
# 「一、总体要求」「四、工作要求」等章节标题（无「第×章」时仍要当结构头）
SECTION_CN_HEAD = re.compile(r"^[一二三四五六七八九十百千零〇]+、")
TABLE_HTML = re.compile(r"<table[\s>]", re.I)
TABLE_BLOCK = re.compile(r"<table[\s\S]*?</table>", re.I)
TABLE_ROW_END = re.compile(r"</tr>", re.I)
TD_CELL = re.compile(r"<td[^>]*>(.*?)</td>", re.I | re.S)
META_TAG = re.compile(r"【([^】]+)】([^【\n]*)")
META_HEADER_LINE = re.compile(r"【[^】]+】")
WENHAO_TAG = re.compile(r"【文号】([^【\n]+)")
SEP = "\n##\n"


@dataclass
class DocMetadata:
    wenhao: str = ""
    header_block: str = ""
    fields: dict[str, str] = field(default_factory=dict)


@dataclass
class Sentence:
    text: str
    tokens: int


@dataclass
class ChunkResult:
    rel_path: str
    n_sents: int
    n_chunks: int
    chunk_tokens: list[int] = field(default_factory=list)
    n_cuts_semantic: int = 0
    n_cuts_hard: int = 0
    n_enforce_splits: int = 0
    n_emergency_splits: int = 0
    n_ge_512: int = 0
    tau: float = 0.0
    sim_p20: float = 0.0
    error: str = ""
    bytes: int = 0
    elapsed_sec: float = 0.0


_LOG_LOCK = threading.Lock()


def log(msg: str) -> None:
    """带时间戳的线程安全日志，立即 flush（服务器 tee 也能实时看到）。"""
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with _LOG_LOCK:
        print(f"[{ts}] {msg}", flush=True)


def fmt_dur(sec: float) -> str:
    """可读时长；<10s 保留小数，避免 build=0s。"""
    if sec is None or sec < 0 or sec != sec:  # NaN
        return "?"
    if sec < 10:
        return f"{sec:.2f}s"
    sec_i = int(round(sec))
    if sec_i < 60:
        return f"{sec_i}s"
    m, s = divmod(sec_i, 60)
    if m < 60:
        return f"{m}m{s:02d}s"
    h, m = divmod(m, 60)
    return f"{h}h{m:02d}m{s:02d}s"


def fmt_size(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n/1024:.1f}KB"
    return f"{n/1024/1024:.2f}MB"


class Pace:
    """基于实时 embedding 吞吐算 ETA（墙钟总吞吐 + 单批 EWMA）。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.texts_done = 0
        self.t_start: float | None = None
        self.batch_ewma: float | None = None  # 单批瞬时吞吐 EWMA
        self.wall_rate: float | None = None  # texts_done / 墙钟（并发下更准）

    def note(self, n_texts: int, elapsed: float) -> None:
        if n_texts <= 0 or elapsed <= 0:
            return
        with self._lock:
            now = time.time()
            if self.t_start is None:
                self.t_start = now - elapsed
            self.texts_done += n_texts
            wall = max(1e-6, now - self.t_start)
            self.wall_rate = self.texts_done / wall
            inst = n_texts / elapsed
            if self.batch_ewma is None:
                self.batch_ewma = inst
            else:
                self.batch_ewma = 0.6 * self.batch_ewma + 0.4 * inst

    def rate(self) -> float | None:
        """优先用墙钟吞吐（含并发）；尚未有样本则 None。"""
        with self._lock:
            if self.wall_rate and self.wall_rate > 0:
                return self.wall_rate
            if self.batch_ewma and self.batch_ewma > 0:
                return self.batch_ewma
            return None

    def eta(self, remaining_texts: int) -> float | None:
        r = self.rate()
        if r is None or r <= 0 or remaining_texts <= 0:
            return None if r is None else 0.0
        return remaining_texts / r

    def snapshot(self) -> tuple[int, float | None, float, float | None]:
        """texts_done, wall_rate, elapsed, batch_ewma"""
        with self._lock:
            elapsed = (time.time() - self.t_start) if self.t_start else 0.0
            return self.texts_done, self.wall_rate, elapsed, self.batch_ewma


def fmt_eta(sec: float | None) -> str:
    return "?" if sec is None else fmt_dur(sec)


def probe_embedding_rate(emb: "Embedder", n: int = 16) -> float | None:
    """启动时实测一批 embedding，给 Pace 灌入真实初速。"""
    n = max(1, min(n, emb.batch_size))
    # 近似窗口长度，避免测速和真实窗差太大
    samples = [f"语义分块吞吐探测{i}。" + ("铁路规章制度条款内容" * 8) for i in range(n)]
    log(f"[probe] 实测 embedding 吞吐… n={n} model={emb.model}")
    t0 = time.time()
    try:
        emb.embed_many(samples)
    except Exception as e:
        log(f"[probe] FAIL {type(e).__name__}: {e}")
        return None
    dt = time.time() - t0
    rate = n / dt if dt > 0 else None
    log(
        f"[probe] 完成 n={n} cost={dt:.2f}s → "
        f"{rate:.2f} txt/s（后续 ETA 全部按实时吞吐滚动，不设固定值）"
        if rate
        else f"[probe] 完成但耗时异常 dt={dt}"
    )
    return rate


class Tokenizer:
    def __init__(self, name: str, prefer_hf: bool = False, local_path: str = ""):
        self._tok = None
        self.name = name
        self.mode = "char"
        if prefer_hf:
            try:
                from transformers import AutoTokenizer

                src = local_path.strip() if local_path and Path(local_path).exists() else name
                local_only = bool(local_path and Path(local_path).exists())
                self._tok = AutoTokenizer.from_pretrained(src, local_files_only=local_only)
                self.mode = "hf"
                log(f"[tokenizer] loaded {src} (mode=hf)")
            except Exception as e:
                log(f"[tokenizer] HF unavailable → char≈1token ({type(e).__name__})")
        else:
            log("[tokenizer] char≈1token（prefer_hf=false）")

    def count(self, text: str) -> int:
        if not text:
            return 0
        if self._tok is not None:
            return max(1, len(self._tok.encode(text, add_special_tokens=False)))
        return max(1, len(text))

    def head(self, text: str, n_tokens: int) -> str:
        """取文本开头约 n_tokens（送 embedding 截断）。"""
        if not text or n_tokens <= 0:
            return ""
        if self._tok is not None:
            ids = self._tok.encode(text, add_special_tokens=False)
            if len(ids) <= n_tokens:
                return text
            return self._tok.decode(ids[:n_tokens], skip_special_tokens=True)
        return text[:n_tokens]

    def tail(self, text: str, n_tokens: int) -> str:
        """取文本末尾约 n_tokens（用于 hard split overlap）。"""
        if not text or n_tokens <= 0:
            return ""
        if self._tok is not None:
            ids = self._tok.encode(text, add_special_tokens=False)
            if len(ids) <= n_tokens:
                return text
            return self._tok.decode(ids[-n_tokens:], skip_special_tokens=True)
        return text[-n_tokens:]


class Embedder:
    # bce/bge 单条上限约 512；整批过大仍会 413（见 T7/1.txt n=23）
    def __init__(
        self,
        base_url: str,
        model: str,
        batch_size: int,
        timeout: int,
        pace: Pace | None = None,
        tok: Tokenizer | None = None,
        max_input_tokens: int = 504,
        max_batch_chars: int = 6000,
    ):
        self.url = base_url.rstrip("/") + "/embeddings"
        self.model = model
        self.batch_size = batch_size
        self.timeout = timeout
        self.cache: dict[str, list[float]] = {}
        self.n_calls = 0
        self.n_texts = 0
        self._lock = threading.Lock()
        self._session = requests.Session()
        self.pace = pace
        self.tok = tok
        self.max_input_tokens = max(64, int(max_input_tokens))
        # 整批字符预算：预防网关/服务对「多条长窗」直接 413（T7 日志 n=23 崩）
        self.max_batch_chars = max(500, int(max_batch_chars))

    def _truncate(self, text: str, max_tokens: int | None = None) -> str:
        lim = self.max_input_tokens if max_tokens is None else max(64, int(max_tokens))
        if not text:
            return " "
        t = text.strip() or " "
        if self.tok is not None:
            if self.tok.count(t) <= lim:
                return t
            return self.tok.head(t, lim) or " "
        if len(t) <= lim:
            return t
        return t[:lim]

    def embed_many(
        self,
        texts: list[str],
        on_batch: Callable[[int, int, int, float], None] | None = None,
    ) -> list[list[float]]:
        """on_batch(batch_i, n_batches, batch_size, batch_sec)"""
        out: list[list[float] | None] = [None] * len(texts)
        miss_idx: list[int] = []
        miss_txt: list[str] = []
        with self._lock:
            for i, t in enumerate(texts):
                key = t.strip()
                if key in self.cache:
                    out[i] = self.cache[key]
                else:
                    miss_idx.append(i)
                    miss_txt.append(key if key else " ")

        n_miss = len(miss_txt)
        # 先按条数切，再按截断后字符预算细切（对齐服务器 413 边界）
        raw_batches: list[list[str]] = []
        for start in range(0, n_miss, self.batch_size):
            raw_batches.extend(
                self._split_by_char_budget(miss_txt[start : start + self.batch_size])
            )
        n_batches = len(raw_batches)
        done = 0
        for bi, batch in enumerate(raw_batches):
            t1 = time.time()
            vecs = self._post(batch)
            dt = time.time() - t1
            with self._lock:
                self.n_calls += 1
                self.n_texts += len(batch)
                for j, v in enumerate(vecs):
                    gi = miss_idx[done + j]
                    self.cache[miss_txt[done + j]] = v
                    out[gi] = v
            done += len(batch)
            if self.pace is not None:
                self.pace.note(len(batch), dt)
            if on_batch is not None:
                on_batch(bi + 1, n_batches, len(batch), dt)
        return [x if x is not None else [] for x in out]

    def _split_by_char_budget(self, texts: list[str]) -> list[list[str]]:
        """把一批文本按截断后总字符数拆成若干小批，降低 413 概率。"""
        if not texts:
            return []
        out: list[list[str]] = []
        cur: list[str] = []
        cur_chars = 0
        for t in texts:
            piece = self._truncate(t)
            n = max(1, len(piece))
            if cur and cur_chars + n > self.max_batch_chars:
                out.append(cur)
                cur = []
                cur_chars = 0
            cur.append(t)
            cur_chars += n
            # 单条已超预算：仍单独成批（交给 413 单条截断兜底）
            if cur_chars > self.max_batch_chars and len(cur) > 1:
                out.append(cur[:-1])
                cur = [t]
                cur_chars = n
        if cur:
            out.append(cur)
        return out

    def _is_payload_too_large(self, e: Exception, resp: Any = None) -> bool:
        msg = str(e)
        if "413" in msg or "Payload Too Large" in msg or "Request Entity Too Large" in msg:
            return True
        code = getattr(resp, "status_code", None)
        return code == 413

    def _post_once(self, inputs: list[str], max_tokens: int | None = None) -> list[list[float]]:
        lim = self.max_input_tokens if max_tokens is None else max_tokens
        payload = [self._truncate(x, lim) for x in inputs]
        r = self._session.post(
            self.url,
            json={"model": self.model, "input": payload},
            timeout=self.timeout,
        )
        if r.status_code == 413:
            raise requests.exceptions.HTTPError(
                f"413 Client Error: Payload Too Large for url: {self.url}",
                response=r,
            )
        r.raise_for_status()
        data = r.json().get("data") or []
        data = sorted(data, key=lambda x: x.get("index", 0))
        return [d["embedding"] for d in data]

    def _post(self, inputs: list[str], max_tokens: int | None = None) -> list[list[float]]:
        """413 时拆半重试；单条仍 413 则再按 token 砍半。其它错误指数退避。"""
        if not inputs:
            return []
        lim = self.max_input_tokens if max_tokens is None else max(64, int(max_tokens))
        for attempt in range(4):
            t0 = time.time()
            try:
                return self._post_once(inputs, lim)
            except Exception as e:
                dt = time.time() - t0
                resp = getattr(e, "response", None)
                if self._is_payload_too_large(e, resp):
                    if len(inputs) > 1:
                        mid = max(1, len(inputs) // 2)
                        log(
                            f"[embed] 413 → 拆批 {len(inputs)}→{mid}+{len(inputs)-mid}"
                        )
                        left = self._post(inputs[:mid], lim)
                        right = self._post(inputs[mid:], lim)
                        return left + right
                    cut = max(64, lim // 2)
                    log(f"[embed] 413 单条 → 截断 {lim}→{cut} tokens 重试")
                    return self._post(inputs, cut)
                if attempt == 3:
                    log(
                        f"[embed] FAIL after 4 tries n={len(inputs)} "
                        f"last={dt:.1f}s {type(e).__name__}: {e}"
                    )
                    raise
                wait = 1.5 * (attempt + 1)
                log(
                    f"[embed] retry {attempt+1}/3 n={len(inputs)} "
                    f"err={type(e).__name__} cost={dt:.1f}s sleep={wait:.1f}s"
                )
                time.sleep(wait)
        return []


def cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 1.0
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(y * y for y in b) ** 0.5
    if na < 1e-12 or nb < 1e-12:
        return 1.0
    return float(dot / (na * nb))


def _metadata_cfg(cfg: dict[str, Any] | None) -> dict[str, bool]:
    mc = (cfg or {}).get("metadata") or {}
    return {
        "enabled": bool(mc.get("enabled", True)),
        "strip_repeated_wenhao": bool(mc.get("strip_repeated_wenhao", True)),
        "header_on_first_chunk": bool(mc.get("header_on_first_chunk", True)),
        "wenhao_line_other_chunks": bool(mc.get("wenhao_line_other_chunks", True)),
    }


def parse_meta_fields(text: str) -> dict[str, str]:
    fields: dict[str, str] = {}
    for m in META_TAG.finditer(text):
        key = m.group(1).strip()
        val = m.group(2).strip()
        if key and val and key not in fields:
            fields[key] = val
    return fields


def extract_header_and_body(raw: str) -> tuple[str, str]:
    """分离文首【知识库分类】等元数据块与正文。"""
    lines = raw.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    header_buf: list[str] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].strip()
        if not header_buf and not stripped:
            i += 1
            continue
        if not header_buf and not META_HEADER_LINE.search(stripped):
            break
        if header_buf and stripped == "---":
            i += 1
            break
        if META_HEADER_LINE.search(stripped):
            header_buf.append(lines[i])
            i += 1
            continue
        if header_buf:
            break
        break
    header = "\n".join(header_buf).strip()
    body = "\n".join(lines[i:]).strip()
    return header, body


def strip_repeated_wenhao(text: str, wenhao: str) -> tuple[str, int]:
    """删除正文中与主文号相同的 【文号】标签（保留他文引用）。"""
    wenhao = wenhao.strip()
    if not wenhao or not text:
        return text, 0
    before = len(WENHAO_TAG.findall(text))
    esc = re.escape(wenhao)
    text = re.sub(rf"(?m)^\s*【文号】\s*{esc}\s*$", "", text)
    text = re.sub(rf"---\s*【文号】\s*{esc}\s*", "---\n", text)
    text = re.sub(rf"【文号】\s*{esc}\s*", "", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    after = len(WENHAO_TAG.findall(text))
    return text.strip(), max(0, before - after)


def preprocess_metadata(raw: str, cfg: dict[str, Any] | None = None) -> tuple[str, DocMetadata, dict[str, int]]:
    """
    切分前：抽出文档头、正文去掉重复【文号】。
    返回 (正文, metadata, stats)。
    """
    mc = _metadata_cfg(cfg)
    stats = {"wenhao_stripped": 0}
    meta = DocMetadata()
    if not mc["enabled"]:
        return raw, meta, stats

    header, body = extract_header_and_body(raw)
    if header:
        meta.header_block = header
        meta.fields = parse_meta_fields(header)
        meta.wenhao = meta.fields.get("文号", "").strip()

    if not meta.wenhao:
        m = WENHAO_TAG.search(raw[:4000])
        if m:
            meta.wenhao = m.group(1).strip()

    if mc["strip_repeated_wenhao"] and meta.wenhao:
        body, stats["wenhao_stripped"] = strip_repeated_wenhao(body, meta.wenhao)

    return body, meta, stats


def compact_header_block(meta: DocMetadata) -> str:
    """首块加完整头后超 512 时用的短头。"""
    lines: list[str] = []
    if meta.wenhao:
        lines.append(f"【文号】{meta.wenhao}")
    if meta.fields.get("标题"):
        lines.append(f"【标题】{meta.fields['标题']}")
    if meta.fields.get("知识库分类"):
        lines.append(f"【知识库分类】{meta.fields['知识库分类']}")
    return "\n".join(lines)


def attach_metadata_to_parts(
    parts: list[str],
    meta: DocMetadata,
    tok: Tokenizer,
    cfg: dict[str, Any] | None,
) -> list[str]:
    """首块写完整文档头；其余块可选一行「文号：」。"""
    mc = _metadata_cfg(cfg)
    if not parts or not mc["enabled"]:
        return parts

    out = list(parts)
    absolute_max = int((cfg or {}).get("chunk", {}).get("absolute_max_tokens") or 512)

    if mc["header_on_first_chunk"] and meta.header_block:
        head = meta.header_block
        combined = f"{head}\n\n{out[0].lstrip()}".strip()
        if tok.count(combined) >= absolute_max:
            head = compact_header_block(meta) or head
            combined = f"{head}\n\n{out[0].lstrip()}".strip()
        out[0] = combined

    if mc["wenhao_line_other_chunks"] and meta.wenhao:
        prefix = f"文号：{meta.wenhao}"
        for i in range(1, len(out)):
            if out[i].startswith("文号：") or out[i].startswith("【文号】"):
                continue
            out[i] = f"{prefix}\n{out[i].lstrip()}"

    return out


def finalize_output_parts(
    parts: list[str],
    meta: DocMetadata,
    tok: Tokenizer,
    cfg: dict[str, Any],
    tag: str = "",
    emb: Embedder | None = None,
) -> tuple[list[str], dict[str, int]]:
    """finalize 后挂 metadata，再验 512；emergency 后再 merge 一次防碎块。"""
    stats: dict[str, int] = {"n_ge_512": 0, "emergency_splits": 0, "post_merged": 0}
    if not parts:
        return [], stats

    parts = attach_metadata_to_parts(parts, meta, tok, cfg)
    absolute_max = int(cfg.get("chunk", {}).get("absolute_max_tokens") or 512)
    hard_max = int(cfg.get("chunk", {}).get("hard_max_tokens") or 495)
    min_keep = int(cfg.get("chunk", {}).get("min_keep_tokens") or 50)
    viol = validate_parts(parts, tok, absolute_max)
    if viol:
        log(f"{tag}[meta] attach 后 {len(viol)} 块≥{absolute_max}，emergency split")
        parts, stats["emergency_splits"] = emergency_split_parts(parts, tok, absolute_max)
        viol = validate_parts(parts, tok, absolute_max)
        if emb is not None and min_keep > 0:
            before = len(parts)
            parts, mstats = merge_small_chunks(parts, tok, emb, min_keep, hard_max)
            stats["post_merged"] = mstats.get("merged", 0)
            if before != len(parts):
                log(f"{tag}[meta] emergency 后再 merge {before}→{len(parts)}")
            viol = validate_parts(parts, tok, absolute_max)
    stats["n_ge_512"] = len(viol)
    return parts, stats


def _structure_cfg(cfg: dict[str, Any] | None) -> dict[str, bool]:
    sc = (cfg or {}).get("structure_split") or {}
    return {
        "article": bool(sc.get("article", True)),
        "chapter": bool(sc.get("chapter", True)),
        "section_cn": bool(sc.get("section_cn", True)),
        "enum_cn": bool(sc.get("enum_cn", True)),
        "enum_num": bool(sc.get("enum_num", True)),
    }


def insert_structure_breaks(text: str, cfg: dict[str, Any] | None = None) -> str:
    """在章/节/条/列举项前插入换行，便于分句。"""
    sc = _structure_cfg(cfg)
    if sc["chapter"]:
        text = re.sub(r"(?<!\n)(第[一二三四五六七八九十百千零〇\d]+章)", r"\n\1", text)
    if sc["article"]:
        text = re.sub(r"(?<!\n)(第[一二三四五六七八九十百千零〇\d]+条)", r"\n\1", text)
    if sc["section_cn"]:
        text = re.sub(
            r"(?<!\n)([一二三四五六七八九十百千零〇]+、)",
            r"\n\1",
            text,
        )
    if sc["enum_cn"]:
        text = re.sub(r"(?<=[。！？；])(（[一二三四五六七八九十百千零〇]+）)", r"\n\1", text)
        text = re.sub(r"(?<!\n)(（[一二三四五六七八九十百千零〇]+）)", r"\n\1", text)
    if sc["enum_num"]:
        text = re.sub(r"(?<=[。！？；\n])(\d+[．.])", r"\n\1", text)
    return text


def _structure_first_line(text: str) -> str:
    """取首行并去掉 Markdown 标题井号（源 MD 常见 `## 三、…`）。"""
    t = (text or "").strip()
    if not t:
        return ""
    first = t.split("\n", 1)[0].strip()
    return re.sub(r"^#{1,6}\s*", "", first).strip()


def _structure_head_level(text: str) -> int:
    """
    结构头强度（系统性层级，不看业务字段名）：
    0=非结构头；1=数字条（1．）；2=中文枚举（（一））；3=章/节/条（第×章、一、、第×条）。
    """
    first = _structure_first_line(text)
    if not first:
        return 0
    if CHAPTER_HEAD.match(first) or CLAUSE_HEAD.match(first) or SECTION_CN_HEAD.match(first):
        return 3
    if ENUM_CN_HEAD.match(first):
        return 2
    if ENUM_NUM_HEAD.match(first):
        return 1
    return 0


def _is_structure_head(text: str) -> bool:
    """句首是否为结构头（章/节/条/（一）/1．）；允许多行句时看首行。"""
    return _structure_head_level(text) > 0


def _merge_sentence_parts(
    parts: list[str],
    tok: Tokenizer,
    min_sent_tokens: int = 40,
) -> list[str]:
    """
    按长度粘短句（不看内容）：非结构头连续短行累加到 ≥ min_sent_tokens 再落盘。
    解决换行分句后大量短行各自成缝、再被语义/硬切撕碎的共性问题。
    """
    floor = max(8, int(min_sent_tokens))
    merged: list[str] = []
    buf = ""
    for p in parts:
        p = p.strip()
        if not p:
            continue
        if _is_structure_head(p):
            if buf:
                merged.append(buf)
                buf = ""
            merged.append(p)
            continue
        cand = (buf + "\n" + p).strip() if buf else p
        if tok.count(cand) < floor:
            buf = cand
            continue
        merged.append(cand)
        buf = ""
    if buf:
        if merged and not _is_structure_head(merged[-1]):
            merged[-1] = (merged[-1] + "\n" + buf).strip()
        else:
            merged.append(buf)
    return merged


def split_text_to_parts(text: str, tok: Tokenizer, cfg: dict[str, Any] | None = None) -> list[str]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"\n#{1,6}\s*\n", "\n", text)
    # 行首 Markdown 标题井号去掉，避免 `## 三、` 识别不成结构头
    text = re.sub(r"(?m)^#{1,6}\s+", "", text)
    text = insert_structure_breaks(text, cfg)
    parts = [p.strip() for p in SENT_SPLIT.split(text) if p and p.strip()]
    # 短句地板：默认用 min_keep；过短行先粘成句，再进滑动窗
    min_sent = int((cfg or {}).get("chunk", {}).get("min_keep_tokens") or 50)
    min_sent = max(24, min(80, min_sent))
    return _merge_sentence_parts(parts, tok, min_sent_tokens=min_sent)


def _strip_html_cell(text: str) -> str:
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def html_table_to_plaintext(table_html: str) -> str:
    """<table> → [表格] + 每行「列 | 列 | …」纯文本。"""
    lines: list[str] = []
    for tr in TABLE_ROW_END.split(table_html):
        cells = [_strip_html_cell(c) for c in TD_CELL.findall(tr)]
        cells = [c for c in cells if c]
        if cells:
            lines.append(" | ".join(cells))
    if not lines:
        return _strip_html_cell(table_html)
    return "[表格]\n" + "\n".join(lines)


def split_table_plaintext(plain: str, tok: Tokenizer, hard_max: int) -> list[str]:
    """纯文本表格按行累积切分，超长行再 token 窗切（无 overlap）。"""
    plain = plain.strip()
    if not plain:
        return []
    if tok.count(plain) <= hard_max:
        return [plain]

    chunks: list[str] = []
    buf = ""
    for line in plain.split("\n"):
        line = line.strip()
        if not line:
            continue
        if tok.count(line) > hard_max:
            if buf.strip():
                chunks.append(buf.strip())
                buf = ""
            chunks.extend(_token_window_split(line, tok, hard_max, overlap=0))
            continue
        cand = f"{buf}\n{line}".strip() if buf else line
        if buf and tok.count(cand) > hard_max:
            chunks.append(buf.strip())
            buf = line
        else:
            buf = cand
    if buf.strip():
        if tok.count(buf) > hard_max:
            chunks.extend(_token_window_split(buf, tok, hard_max, overlap=0))
        else:
            chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]


def split_table_html(
    table_html: str,
    tok: Tokenizer,
    hard_max: int,
    overlap: int = 0,
    *,
    to_plaintext: bool = True,
) -> list[str]:
    """HTML 表格：默认转纯文本后按行切分。"""
    if to_plaintext:
        return split_table_plaintext(html_table_to_plaintext(table_html), tok, hard_max)
    # 遗留：原始 HTML 按行切（不推荐）
    rows = TABLE_ROW_END.split(table_html)
    chunks: list[str] = []
    buf = ""
    for i, row in enumerate(rows):
        piece = row if i == 0 else row + "</tr>"
        cand = (buf + piece) if buf else piece
        if buf and tok.count(cand) > hard_max:
            chunks.append(buf.strip())
            buf = piece
            if tok.count(buf) > hard_max:
                buf_chunks = _token_window_split(buf, tok, hard_max, 0)
                if len(buf_chunks) > 1:
                    chunks.extend(buf_chunks[:-1])
                    buf = buf_chunks[-1]
        else:
            buf = cand
    if buf.strip():
        if tok.count(buf) > hard_max:
            chunks.extend(_token_window_split(buf, tok, hard_max, 0))
        else:
            chunks.append(buf.strip())
    return [c for c in chunks if c.strip()]


def _token_window_split(text: str, tok: Tokenizer, hard_max: int, overlap: int) -> list[str]:
    """无结构切点时的 token 窗硬切（overlap 计入每块总长）。"""
    text = text.strip()
    if not text or tok.count(text) <= hard_max:
        return [text] if text else []
    body_max = max(32, hard_max - overlap) if overlap > 0 else hard_max
    out: list[str] = []
    rest = text
    prev_tail = ""
    while rest.strip():
        if tok.count(rest) <= hard_max:
            out.append((prev_tail + rest).strip())
            break
        lo, hi = 1, len(rest)
        best = 1
        while lo <= hi:
            mid = (lo + hi) // 2
            seg = rest[:mid]
            piece = (prev_tail + seg) if prev_tail else seg
            if tok.count(piece) <= hard_max:
                best = mid
                lo = mid + 1
            else:
                hi = mid - 1
        seg = rest[:best]
        out.append(((prev_tail + seg) if prev_tail else seg).strip())
        rest = rest[best:]
        prev_tail = tok.tail(out[-1], overlap) if overlap > 0 and rest.strip() else ""
    return [x for x in out if x.strip()]


def pre_split_oversized_sentences(
    sents: list[Sentence],
    tok: Tokenizer,
    hard_max: int,
    overlap: int = 0,
    tag: str = "",
) -> list[Sentence]:
    """
    分句后、embedding 前：把单句 > hard_max 的文本按 token 硬切成多段。
    避免整句进滑动窗/送 embedding 时被静默截断或触发 413。
    """
    if not sents or hard_max <= 0:
        return sents
    out: list[Sentence] = []
    n_split = 0
    n_parts = 0
    for s in sents:
        n = tok.count(s.text)
        if n <= hard_max:
            out.append(s)
            continue
        parts = _token_window_split(s.text, tok, hard_max, overlap)
        if len(parts) <= 1:
            out.append(s)
            continue
        n_split += 1
        n_parts += len(parts)
        for p in parts:
            out.append(Sentence(p, tok.count(p)))
    if n_split and tag:
        prefix = tag if tag.startswith("[") else f"[{tag}]"
        log(
            f"{prefix} pre_split 超长句 {n_split} 条 → {n_parts} 段 "
            f"(hard_max={hard_max})"
        )
    return out


def split_sentences(text: str, tok: Tokenizer, cfg: dict[str, Any] | None = None) -> list[Sentence]:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    tc = (cfg or {}).get("table") or {}
    detect_table = bool(tc.get("detect_html", True))
    hard_max = int((cfg or {}).get("chunk", {}).get("hard_max_tokens") or 495)
    table_overlap = int(tc.get("overlap_tokens") or 0)
    to_plaintext = bool(tc.get("to_plaintext", True))

    sents: list[Sentence] = []
    if detect_table and TABLE_HTML.search(text):
        pos = 0
        for m in TABLE_BLOCK.finditer(text):
            if m.start() > pos:
                for t in split_text_to_parts(text[pos : m.start()], tok, cfg):
                    sents.append(Sentence(t, tok.count(t)))
            for t in split_table_html(
                m.group(0), tok, hard_max, table_overlap, to_plaintext=to_plaintext
            ):
                sents.append(Sentence(t, tok.count(t)))
            pos = m.end()
        if pos < len(text):
            for t in split_text_to_parts(text[pos:], tok, cfg):
                sents.append(Sentence(t, tok.count(t)))
    else:
        for t in split_text_to_parts(text, tok, cfg):
            sents.append(Sentence(t, tok.count(t)))
    return [s for s in sents if s.text.strip()]


def window_text(sents: list[Sentence], end: int, target: int, min_t: int, max_t: int, reverse: bool) -> str:
    """取边界左侧(end-1 往前)或右侧(end 往后)的窗口文本。"""
    if reverse:
        i = end - 1
        acc = 0
        chunks: list[str] = []
        while i >= 0 and acc < target:
            chunks.append(sents[i].text)
            acc += sents[i].tokens
            if acc >= max_t:
                break
            i -= 1
        if acc < min_t and i >= 0:
            while i >= 0 and acc < min_t:
                chunks.append(sents[i].text)
                acc += sents[i].tokens
                i -= 1
        return "".join(reversed(chunks))
    i = end
    acc = 0
    chunks = []
    while i < len(sents) and acc < target:
        chunks.append(sents[i].text)
        acc += sents[i].tokens
        if acc >= max_t:
            break
        i += 1
    if acc < min_t and i < len(sents):
        while i < len(sents) and acc < min_t:
            chunks.append(sents[i].text)
            acc += sents[i].tokens
            i += 1
    return "".join(chunks)


def estimate_doc_work(n_sents: int, batch_size: int) -> tuple[int, int, int]:
    """返回 (句缝数, 待 embed 文本数≈2*缝, 批次数)。"""
    seams = max(0, n_sents - 1)
    texts = seams * 2
    batches = 0 if texts == 0 else 2 * ((seams + batch_size - 1) // batch_size)
    return seams, texts, batches


def neighbor_sims(
    sents: list[Sentence],
    emb: Embedder,
    win_cfg: dict[str, Any],
    doc_tag: str = "",
) -> list[float]:
    """句缝相似度。doc_tag 用于日志前缀，如 [pass1 3/15]。"""
    n = len(sents)
    if n < 2:
        return []
    seams, n_texts, n_batches = estimate_doc_work(n, emb.batch_size)
    lefts: list[str] = []
    rights: list[str] = []
    t_doc = time.time()
    t_build = time.time()
    # 本篇剩余 embed 文本（左+右）；随 batch 递减用于 ETA
    remain_box = {"n": n_texts}

    def _pace_bits(extra_remain: int = 0) -> str:
        """已用总时间 + 当前吞吐 + 预估剩余。"""
        if emb.pace is None:
            return f"已用={fmt_dur(time.time()-t_doc)} ETA=?"
        _done, wall_rate, elapsed, _ewma = emb.pace.snapshot()
        rate_s = f"{wall_rate:.1f}txt/s" if wall_rate else "?"
        eta = emb.pace.eta(max(0, remain_box["n"] + extra_remain))
        return f"已用总={fmt_dur(elapsed)} 本篇已用={fmt_dur(time.time()-t_doc)} rate={rate_s} ETA剩={fmt_eta(eta)}"

    for boundary in range(1, n):
        lefts.append(
            window_text(
                sents,
                boundary,
                win_cfg["target_tokens"],
                win_cfg["min_tokens"],
                win_cfg["max_tokens"],
                reverse=True,
            )
        )
        rights.append(
            window_text(
                sents,
                boundary,
                win_cfg["target_tokens"],
                win_cfg["min_tokens"],
                win_cfg["max_tokens"],
                reverse=False,
            )
        )
        # 每 50 个句缝报一次建窗进度（大文档不哑巴）
        if boundary % 50 == 0 or boundary == n - 1:
            log(
                f"{doc_tag} 建窗 句缝 {boundary}/{seams} "
                f"({100*boundary/seams:.0f}%) build={fmt_dur(time.time()-t_build)} | {_pace_bits()}"
            )

    def make_cb(side: str) -> Callable[[int, int, int, float], None]:
        def _cb(bi: int, nb: int, bsz: int, dt: float) -> None:
            remain_box["n"] = max(0, remain_box["n"] - bsz)
            rate = bsz / dt if dt > 0 else 0
            log(
                f"{doc_tag} embed-{side} batch {bi}/{nb} "
                f"n={bsz} cost={dt:.2f}s 本批={rate:.1f}txt/s "
                f"global_calls={emb.n_calls} cache={len(emb.cache)} | {_pace_bits()}"
            )

        return _cb

    n_left_batches = max(1, (seams + emb.batch_size - 1) // emb.batch_size) if seams else 0
    log(
        f"{doc_tag} 开始 embedding 左窗 seams={seams} texts≈{seams} "
        f"batches≈{n_left_batches} | {_pace_bits()}"
    )
    lv = emb.embed_many(lefts, on_batch=make_cb("L"))
    log(
        f"{doc_tag} 开始 embedding 右窗 seams={seams} texts≈{seams} "
        f"batches≈{n_left_batches} | {_pace_bits()}"
    )
    rv = emb.embed_many(rights, on_batch=make_cb("R"))
    sims = [cosine(a, b) for a, b in zip(lv, rv)]
    log(
        f"{doc_tag} 相似度算完 seams={len(sims)} texts={n_texts} "
        f"planned_batches={n_batches} 本篇耗时={fmt_dur(time.time()-t_doc)} | {_pace_bits()}"
    )
    return sims


def calibrate_tau(all_sims: list[float], percentile: float) -> float:
    if not all_sims:
        return 0.7
    xs = sorted(all_sims)
    k = max(0, min(len(xs) - 1, int(len(xs) * percentile / 100.0)))
    return float(xs[k])


def compute_local_drops(sims: list[float], radius: int = 3) -> list[float]:
    """每个缝：邻域中位数 - 本缝 sim（仅保留正跌幅）。中位数本身就是动态的。"""
    drops: list[float] = []
    n = len(sims)
    for i, sim in enumerate(sims):
        lo = max(0, i - radius)
        hi = min(n, i + radius + 1)
        local = sims[lo:hi]
        if not local:
            continue
        med = statistics.median(local)
        d = med - sim
        if d > 1e-9:
            drops.append(float(d))
    return drops


def calibrate_local_drop(
    all_drops: list[float],
    percentile: float = 80.0,
    floor: float = 0.03,
    ceil: float = 0.25,
    fallback: float = 0.08,
) -> float:
    """
    用本批「正跌幅」分位数标定 local_drop（与 τ 同思路，禁止拍脑袋）。
    例：P80 → 只有跌幅最大的约 20% 缝进入局部异常候选。
    """
    if not all_drops:
        return fallback
    xs = sorted(all_drops)
    k = max(0, min(len(xs) - 1, int(len(xs) * percentile / 100.0)))
    raw = float(xs[k])
    return max(floor, min(ceil, raw))


def resolve_local_drop(cfg: dict[str, Any], all_drops: list[float] | None = None) -> float:
    th = cfg.get("threshold") or {}
    mode = str(th.get("local_drop_mode") or "percentile").lower()
    fallback = float(th.get("local_drop") or 0.08)
    if mode == "fixed":
        return fallback
    return calibrate_local_drop(
        all_drops or [],
        percentile=float(th.get("local_drop_percentile") or 80),
        floor=float(th.get("local_drop_floor") or 0.03),
        ceil=float(th.get("local_drop_ceil") or 0.25),
        fallback=fallback,
    )


def _range_tokens(sents: list[Sentence], a: int, b: int) -> int:
    return sum(s.tokens for s in sents[a:b])


def snap_cut_to_structure(
    cut: int,
    start: int,
    sents: list[Sentence],
    hard_max: int,
    min_tokens: int,
    window: int = 8,
) -> int:
    """
    将临时切点对齐到结构头（系统性，不看字段名）：
    1) 禁止结构头落在上一块末尾；
    2) 优先把切点挪到「下一结构头」之前（完成当前项再切）；
    3) 切在数字条时回退到上级（（一）/一、）；
    4) 否则在窗口内回退到最近强结构头。
    用于消除 ## 相对小节/条目提前或靠后 1～3 行的共性问题。
    """
    n = len(sents)
    if cut <= start or cut >= n:
        return max(start + 1, min(cut, n)) if n > start else cut
    min_left = max(1, min_tokens // 2)
    win = max(1, int(window))

    def ok(c: int) -> bool:
        if c <= start or c > n:
            return False
        left = _range_tokens(sents, start, c)
        return min_left <= left <= hard_max

    def peel_trailing_heads(c: int) -> int:
        """任何对齐之后都再剥块尾孤标题（如回退到（一）后仍挂着「三、」）。"""
        guard = 0
        while (
            guard < 8
            and c - 1 >= start
            and _is_structure_head(sents[c - 1].text)
            and ok(c - 1)
        ):
            c -= 1
            guard += 1
        return c

    cut = peel_trailing_heads(cut)

    # 已对准结构头开头
    if cut < n and _is_structure_head(sents[cut].text) and ok(cut):
        # 若切在「1．/2．」上，优先回退到窗口内更强的「（二）」/「一、」
        if _structure_head_level(sents[cut].text) == 1:
            for h in range(cut - 1, max(start, cut - win) - 1, -1):
                if _structure_head_level(sents[h].text) >= 2 and ok(h):
                    return peel_trailing_heads(h)
        return peel_trailing_heads(cut)

    # 2) 向前：下一结构头（把中间尾巴留在本块，避免拆开项内短行）
    for h in range(cut, min(n, cut + win + 1)):
        if _is_structure_head(sents[h].text) and ok(h):
            if _structure_head_level(sents[h].text) == 1:
                for h2 in range(h - 1, max(start, h - win) - 1, -1):
                    if _structure_head_level(sents[h2].text) >= 2 and ok(h2):
                        return peel_trailing_heads(h2)
            return peel_trailing_heads(h)

    # 3) 向后：最近结构头（强层级优先）
    best_h = -1
    best_lv = 0
    for h in range(cut - 1, max(start, cut - win) - 1, -1):
        lv = _structure_head_level(sents[h].text)
        if lv > 0 and ok(h) and lv >= best_lv:
            best_h = h
            best_lv = lv
    if best_h >= 0:
        return peel_trailing_heads(best_h)

    c2 = cut if ok(cut) else (cut if cut > start else min(start + 1, n))
    return peel_trailing_heads(c2)


def pick_cut_boundaries(
    sims: list[float],
    sents: list[Sentence],
    tau: float,
    local_drop: float,
    hard_max: int,
    target: int,
    min_tokens: int,
) -> tuple[list[int], int, int]:
    """返回切开的句索引（新块从该句开始），以及 semantic/hard 切次数。"""
    n = len(sents)
    if n == 0:
        return [], 0, 0
    candidates: set[int] = set()
    for i, sim in enumerate(sims):
        boundary = i + 1
        if sim <= tau:
            candidates.add(boundary)
        lo = max(0, i - 3)
        hi = min(len(sims), i + 4)
        local = sims[lo:hi]
        if local:
            # 邻域中位数：每缝动态算；门槛 local_drop 由本批分布标定
            med = statistics.median(local)
            if med - sim >= local_drop:
                candidates.add(boundary)
    # 结构头前的缝一律可作候选（长度门闩仍由下方 acc>=min_tokens 约束）
    for i in range(1, n):
        if _is_structure_head(sents[i].text):
            candidates.add(i)

    cuts: list[int] = []
    n_sem = 0
    n_hard = 0
    start = 0
    while start < n:
        acc = 0
        j = start
        last_cand = -1
        while j < n:
            nxt_tok = sents[j].tokens
            # 加入下一句会超硬顶 → 先在 j 处切，避免「多吞一句」
            if acc > 0 and acc + nxt_tok > hard_max:
                cut_at = j if acc >= min_tokens // 2 else min(j + 1, n)
                if cut_at <= start:
                    cut_at = min(start + 1, n)
                cut_at = snap_cut_to_structure(
                    cut_at, start, sents, hard_max, min_tokens
                )
                cuts.append(cut_at)
                n_hard += 1
                start = cut_at
                break
            acc += nxt_tok
            nxt = j + 1
            if nxt < n and nxt in candidates and acc >= min_tokens:
                last_cand = nxt
            if acc >= target and last_cand > start:
                cut_at = snap_cut_to_structure(
                    last_cand, start, sents, hard_max, min_tokens
                )
                cuts.append(cut_at)
                n_sem += 1
                start = cut_at
                break
            if acc >= hard_max:
                best_b = -1
                best_sim = 2.0
                best_struct = -1
                # 硬切：窗口内最靠后的结构头优先，否则最低 sim
                for b in range(start + 1, j + 1):
                    left_ok = _range_tokens(sents, start, b) >= min_tokens // 2
                    if not left_ok:
                        continue
                    if b < n and _is_structure_head(sents[b].text):
                        best_struct = b
                    if b - 1 < len(sims) and sims[b - 1] < best_sim:
                        best_sim = sims[b - 1]
                        best_b = b
                if best_struct >= 0:
                    best_b = best_struct
                if best_b < 0:
                    best_b = j + 1 if j + 1 <= n else j
                    if best_b <= start:
                        best_b = min(start + 1, n)
                best_b = snap_cut_to_structure(
                    best_b, start, sents, hard_max, min_tokens
                )
                cuts.append(best_b)
                n_hard += 1
                start = best_b
                break
            j += 1
        else:
            if last_cand > start and acc > target:
                cut_at = snap_cut_to_structure(
                    last_cand, start, sents, hard_max, min_tokens
                )
                cuts.append(cut_at)
                n_sem += 1
                start = cut_at
                continue
            break
    return cuts, n_sem, n_hard


def _concat_chunks(a: str, b: str) -> str:
    return a.rstrip() + "\n" + b.lstrip()


def merge_small_chunks(
    parts: list[str],
    tok: Tokenizer,
    emb: Embedder,
    min_keep: int,
    hard_max: int,
) -> tuple[list[str], dict[str, int]]:
    """
    < min_keep 的碎块：先看与 prev/next 合并后是否 ≤ hard_max，
    再在可装邻居里用 cosine 选更近的一侧；两边都塞不下则跳过（宁碎不超顶）。
    """
    parts = [p.strip() for p in parts if p and p.strip()]
    stats = {"merged": 0, "skipped": 0}
    if not parts:
        return [], stats

    out = list(parts)
    guard = 0
    while guard < 8:
        guard += 1
        moved = False
        i = 0
        while i < len(out):
            n_i = tok.count(out[i])
            if n_i >= min_keep:
                i += 1
                continue

            cand_prev = _concat_chunks(out[i - 1], out[i]) if i > 0 else None
            cand_next = _concat_chunks(out[i], out[i + 1]) if i + 1 < len(out) else None
            fit_prev = cand_prev is not None and tok.count(cand_prev) <= hard_max
            fit_next = cand_next is not None and tok.count(cand_next) <= hard_max

            if not fit_prev and not fit_next:
                log(
                    f"[merge] skip #{i}({n_i}tok) neither fits "
                    f"(prev={'no' if cand_prev is None else tok.count(cand_prev)} "
                    f"next={'no' if cand_next is None else tok.count(cand_next)} "
                    f"hard_max={hard_max})"
                )
                i += 1
                continue

            texts = [out[i]]
            keys = ["cur"]
            if fit_prev:
                texts.append(out[i - 1])
                keys.append("prev")
            if fit_next:
                texts.append(out[i + 1])
                keys.append("next")
            vecs = emb.embed_many(texts)
            vmap = dict(zip(keys, vecs))
            sim_prev = cosine(vmap["cur"], vmap["prev"]) if fit_prev else -1.0
            sim_next = cosine(vmap["cur"], vmap["next"]) if fit_next else -1.0

            # 同分优先 prev（稳定）
            if fit_prev and (not fit_next or sim_prev >= sim_next):
                size = tok.count(cand_prev)  # type: ignore[arg-type]
                log(f"[merge] #{i}({n_i}tok) → prev sim={sim_prev:.4f} size={size}")
                out[i - 1] = cand_prev  # type: ignore[assignment]
                del out[i]
                stats["merged"] += 1
                moved = True
                continue

            size = tok.count(cand_next)  # type: ignore[arg-type]
            log(f"[merge] #{i}({n_i}tok) → next sim={sim_next:.4f} size={size}")
            out[i] = cand_next  # type: ignore[assignment]
            del out[i + 1]
            stats["merged"] += 1
            moved = True
            # 留在 i，新块可能仍 < min_keep，下轮再处理
            continue

        if not moved:
            break

    stats["skipped"] = sum(1 for p in out if tok.count(p) < min_keep)
    if stats["skipped"]:
        log(f"[merge] 残留碎块 {stats['skipped']} 个（两边合后均会 >{hard_max}，保留不超顶）")
    return out, stats


def _structure_cut_offsets(text: str, cfg: dict[str, Any] | None) -> list[int]:
    """块内结构切点（仅章/节/条/列举；禁止按每个换行撕碎）。"""
    sc = _structure_cfg(cfg)
    pats: list[str] = []
    if sc["chapter"]:
        pats.append(r"第[一二三四五六七八九十百千零〇\d]+章")
    if sc["article"]:
        pats.append(r"第[一二三四五六七八九十百千零〇\d]+条")
    if sc.get("section_cn", True):
        pats.append(r"[一二三四五六七八九十百千零〇]+、")
    if sc["enum_cn"]:
        pats.append(r"（[一二三四五六七八九十百千零〇]+）")
    if sc["enum_num"]:
        pats.append(r"\d+[．.]")
    if not pats:
        return []
    rx = re.compile("|".join(f"(?:{p})" for p in pats))
    return sorted({m.start() for m in rx.finditer(text) if m.start() > 0})


def _sentence_cut_offsets(text: str) -> list[int]:
    """仅句读切点（。！？；），不含换行——避免短行被逐行撕碎。"""
    return sorted(
        {
            m.end()
            for m in re.finditer(r"[。！？；]", text)
            if 0 < m.end() < len(text)
        }
    )


def _split_by_offsets(
    text: str,
    offs: list[int],
    tok: Tokenizer,
    hard_max: int,
    overlap: int,
    min_piece: int = 80,
) -> list[str]:
    """按偏移切；过短片段并入下一段，超 hard_max 再 token 窗。"""
    if not offs:
        return []
    pieces: list[str] = []
    prev = 0
    for off in offs:
        if off <= prev:
            continue
        left = text[prev:off].strip()
        if left:
            pieces.append(left)
        prev = off
    tail = text[prev:].strip()
    if tail:
        pieces.append(tail)
    if len(pieces) <= 1:
        return []

    # 过短片段按长度并入邻段（不看内容）
    glued: list[str] = []
    buf = ""
    for p in pieces:
        cand = (buf + "\n" + p).strip() if buf else p
        if buf and tok.count(cand) <= hard_max and (
            tok.count(buf) < min_piece or tok.count(p) < min_piece // 2
        ):
            buf = cand
            continue
        if buf:
            glued.append(buf)
        buf = p
    if buf:
        glued.append(buf)

    out: list[str] = []
    for p in glued:
        if tok.count(p) <= hard_max:
            out.append(p)
        else:
            out.extend(_token_window_split(p, tok, hard_max, overlap))
    return out if len(out) > 1 else []


def split_oversized_chunk(
    text: str,
    tok: Tokenizer,
    hard_max: int,
    overlap: int,
    cfg: dict[str, Any] | None = None,
) -> list[str]:
    """单块 >hard_max：结构切 → 句读切 → token 窗（绝不按每个 \\n 撕）。"""
    text = text.strip()
    if not text:
        return []
    if tok.count(text) <= hard_max:
        return [text]

    for offs in (
        _structure_cut_offsets(text, cfg),
        _sentence_cut_offsets(text),
    ):
        out = _split_by_offsets(text, offs, tok, hard_max, overlap, min_piece=80)
        if out:
            return out

    return _token_window_split(text, tok, hard_max, overlap)


def enforce_hard_max(
    parts: list[str],
    tok: Tokenizer,
    hard_max: int,
    overlap: int,
    cfg: dict[str, Any] | None = None,
    max_rounds: int = 10,
) -> tuple[list[str], int]:
    """递归 enforce，直到全部 ≤hard_max 或达 max_rounds。"""
    parts = [p.strip() for p in parts if p and p.strip()]
    splits = 0
    for rnd in range(max_rounds):
        nxt: list[str] = []
        moved = False
        for p in parts:
            if tok.count(p) <= hard_max:
                nxt.append(p)
            else:
                moved = True
                subs = split_oversized_chunk(p, tok, hard_max, overlap, cfg)
                splits += max(1, len(subs) - 1)
                nxt.extend(subs)
        parts = nxt
        if not moved:
            break
    return parts, splits


def validate_parts(parts: list[str], tok: Tokenizer, absolute_max: int) -> list[tuple[int, int]]:
    return [(i, tok.count(p)) for i, p in enumerate(parts) if tok.count(p) >= absolute_max]


def emergency_split_parts(
    parts: list[str],
    tok: Tokenizer,
    absolute_max: int,
) -> tuple[list[str], int]:
    """验收仍 ≥absolute_max 时 token 窗兜底（不加 overlap）。"""
    limit = max(32, absolute_max - 8)
    out: list[str] = []
    n_splits = 0
    for p in parts:
        if tok.count(p) < absolute_max:
            out.append(p)
        else:
            subs = _token_window_split(p, tok, limit, overlap=0)
            n_splits += max(1, len(subs) - 1)
            out.extend(subs)
    return out, n_splits


def finalize_parts(
    parts: list[str],
    tok: Tokenizer,
    cfg: dict[str, Any],
    tag: str = "",
    emb: Embedder | None = None,
) -> tuple[list[str], dict[str, int]]:
    """merge 后：enforce → 512 验收 → emergency → 再 merge（防 enforce 撕碎残留）。"""
    chunk_cfg = cfg.get("chunk") or {}
    enforce_cfg = cfg.get("enforce") or {}
    hard_max = int(chunk_cfg.get("hard_max_tokens") or 495)
    absolute_max = int(chunk_cfg.get("absolute_max_tokens") or 512)
    min_keep = int(chunk_cfg.get("min_keep_tokens") or 50)
    overlap = int(enforce_cfg.get("overlap_tokens") or 50)
    enabled = bool(enforce_cfg.get("enabled", True))
    max_rounds = int(enforce_cfg.get("max_rounds") or 10)

    stats = {"enforce_splits": 0, "emergency_splits": 0, "n_ge_512": 0, "post_merged": 0}
    parts = [p.strip() for p in parts if p and p.strip()]
    if not parts:
        return [], stats

    if enabled:
        parts, stats["enforce_splits"] = enforce_hard_max(
            parts, tok, hard_max, overlap, cfg, max_rounds=max_rounds
        )
        if stats["enforce_splits"]:
            log(f"{tag}[enforce] splits={stats['enforce_splits']} hard_max={hard_max}")

    viol = validate_parts(parts, tok, absolute_max)
    if viol:
        log(f"{tag}[validate] {len(viol)} blocks ≥{absolute_max}, emergency split")
        parts, stats["emergency_splits"] = emergency_split_parts(parts, tok, absolute_max)
        viol = validate_parts(parts, tok, absolute_max)

    # enforce/emergency 之后再粘碎块（原先只在 enforce 前 merge，短行会残留）
    if emb is not None and min_keep > 0 and parts:
        before = len(parts)
        parts, mstats = merge_small_chunks(parts, tok, emb, min_keep, hard_max)
        stats["post_merged"] = mstats.get("merged", 0)
        if before != len(parts):
            log(
                f"{tag}[merge-after-enforce] {before}→{len(parts)} "
                f"merged={stats['post_merged']}"
            )
        viol = validate_parts(parts, tok, absolute_max)

    stats["n_ge_512"] = len(viol)
    if viol:
        log(f"{tag}[validate] FAIL still ≥{absolute_max}: {viol[:5]}")
    return parts, stats


def neutralize_hash_in_chunk(chunk: str) -> str:
    """
    块内清除所有 #/## 标记，只保留正文。
    入库 separator='##' / 按 '\\n##\\n' 切时，块内再留 ## 会误切
   （例如「文号## 第七章」没有换行）。
    """
    if not chunk:
        return ""
    # 行首 markdown 标题
    chunk = re.sub(r"(?m)^#{1,6}\s+", "", chunk)
    # 行内粘连的 ## / #
    chunk = re.sub(r"#{1,6}\s*", "", chunk)
    return chunk.strip()


def sanitize_md_separators(text: str) -> str:
    """只保留独立一行的 ## 作为块分隔；压掉连续空分隔。"""
    if not text:
        return ""
    # 连续「单独一行的 ##」→ 单个 \n##\n
    text = re.sub(r"(?:\n##\s*\n)+", "\n##\n", text)
    text = re.sub(r"^##\s*\n+", "", text)
    text = re.sub(r"\n##\s*$", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip() + "\n"


def join_chunks_with_sep(parts: list[str]) -> str:
    """块间一律 \\n##\\n；块内先清掉 #/##，保证按 ##\\n 可稳定切开。"""
    cleaned: list[str] = []
    for p in parts:
        p2 = neutralize_hash_in_chunk(p)
        if p2:
            cleaned.append(p2)
    if not cleaned:
        return ""
    return sanitize_md_separators(SEP.join(cleaned))


def apply_cuts(
    sents: list[Sentence],
    cuts: list[int],
    overlap_tokens: int,
    tok: Tokenizer | None = None,
    emb: Embedder | None = None,
    min_keep: int = 50,
    hard_max: int = 495,
    cfg: dict[str, Any] | None = None,
    doc_meta: DocMetadata | None = None,
    tag: str = "",
) -> tuple[str, list[int], dict[str, int]]:
    """返回 (写出文本, 各块 token, stats)。"""
    stats: dict[str, int] = {"merged": 0, "skipped": 0, "enforce_splits": 0, "emergency_splits": 0, "n_ge_512": 0}
    if not sents:
        return "", [], stats
    bounds = [0] + sorted(set(c for c in cuts if 0 < c < len(sents))) + [len(sents)]
    parts: list[str] = []
    for i in range(len(bounds) - 1):
        a, b = bounds[i], bounds[i + 1]
        chunk = "\n".join(s.text.strip() for s in sents[a:b] if s.text.strip()).strip()
        if overlap_tokens > 0 and i > 0:
            prev = "\n".join(s.text.strip() for s in sents[bounds[i - 1] : a] if s.text.strip())
            ov = prev[-overlap_tokens * 2 :] if prev else ""
            if ov:
                chunk = ov + "\n" + chunk
        if chunk:
            parts.append(chunk)
    if tok is not None and emb is not None and min_keep > 0 and parts:
        before = len(parts)
        parts, mstats = merge_small_chunks(parts, tok, emb, min_keep, hard_max)
        stats.update(mstats)
        log(
            f"{tag}[merge] {before} → {len(parts)} "
            f"(min_keep={min_keep} hard_max={hard_max} "
            f"merged={stats['merged']} skipped={stats['skipped']})"
        )
    if tok is not None and cfg is not None and parts:
        parts, fstats = finalize_parts(parts, tok, cfg, tag=tag, emb=emb)
        stats.update(fstats)
        parts, pstats = finalize_output_parts(
            parts, doc_meta or DocMetadata(), tok, cfg, tag=tag, emb=emb
        )
        stats["emergency_splits"] = stats.get("emergency_splits", 0) + pstats.get("emergency_splits", 0)
        stats["n_ge_512"] = pstats.get("n_ge_512", 0)
    tokens = [tok.count(p) for p in parts] if tok is not None else [len(p) for p in parts]
    return join_chunks_with_sep(parts), tokens, stats


def pick_sample(source: Path, quota: dict[str, int], seed: int) -> list[Path]:
    random.seed(seed)
    by: dict[str, list[Path]] = {}
    for p in source.rglob("*.md"):
        rel = p.relative_to(source)
        cat = rel.parts[0] if len(rel.parts) > 1 else "其他"
        by.setdefault(cat, []).append(p)
    picked: list[Path] = []
    for cat, n in quota.items():
        pool = by.get(cat, [])
        k = min(n, len(pool))
        picked.extend(random.sample(pool, k) if k else [])
    return sorted(picked)


def process_one(
    path: Path,
    source: Path,
    out_root: Path,
    tok: Tokenizer,
    emb: Embedder,
    cfg: dict[str, Any],
    tau: float | None,
    doc_idx: int,
    doc_total: int,
    phase: str,
    local_drop: float | None = None,
) -> tuple[ChunkResult, list[float]]:
    rel = str(path.relative_to(source)).replace("\\", "/")
    tag = f"[{phase} {doc_idx}/{doc_total}]"
    res = ChunkResult(rel_path=rel, n_sents=0, n_chunks=0)
    t0 = time.time()
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        res.bytes = len(raw.encode("utf-8", errors="ignore"))
        body, doc_meta, meta_stats = preprocess_metadata(raw, cfg)
        if meta_stats.get("wenhao_stripped"):
            log(f"{tag} [meta] strip 重复【文号】×{meta_stats['wenhao_stripped']}")
        hard_max = int(cfg.get("chunk", {}).get("hard_max_tokens") or 495)
        sents = split_sentences(body, tok, cfg)
        sents = pre_split_oversized_sentences(
            sents, tok, hard_max, overlap=0, tag=tag.strip()
        )
        res.n_sents = len(sents)
        seams, n_texts, n_batches = estimate_doc_work(len(sents), emb.batch_size)
        eta_doc = emb.pace.eta(n_texts) if emb.pace else None
        rate_now = emb.pace.rate() if emb.pace else None
        log(
            f"{tag} 开始 file={rel} size={fmt_size(res.bytes)} "
            f"sents={len(sents)} seams={seams} embed_texts≈{n_texts} "
            f"batches≈{n_batches} rate={f'{rate_now:.1f}txt/s' if rate_now else '?'} "
            f"ETA≈{fmt_eta(eta_doc)}"
        )

        if len(sents) <= 1:
            parts = [body.strip()] if body.strip() else []
            parts, fin = finalize_parts(parts, tok, cfg, tag=tag + " ", emb=emb)
            parts, post = finalize_output_parts(
                parts, doc_meta, tok, cfg, tag=tag + " ", emb=emb
            )
            fin["emergency_splits"] = fin.get("emergency_splits", 0) + post.get("emergency_splits", 0)
            fin["n_ge_512"] = post.get("n_ge_512", 0)
            text = join_chunks_with_sep(parts)
            out_path = out_root / path.relative_to(source)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
            res.n_chunks = len(parts)
            res.chunk_tokens = [tok.count(p) for p in parts]
            res.n_enforce_splits = fin.get("enforce_splits", 0)
            res.n_emergency_splits = fin.get("emergency_splits", 0)
            res.n_ge_512 = fin.get("n_ge_512", 0)
            if res.n_ge_512:
                res.error = "chunk_exceeds_512"
            res.elapsed_sec = time.time() - t0
            log(
                f"{tag} 短文档 finalize chunks={res.n_chunks} "
                f"max={max(res.chunk_tokens) if res.chunk_tokens else 0} "
                f"cost={fmt_dur(res.elapsed_sec)}"
            )
            return res, []

        sims = neighbor_sims(sents, emb, cfg["window"], doc_tag=tag)
        local_tau = tau if tau is not None else calibrate_tau(sims, cfg["threshold"]["percentile"])
        res.tau = local_tau
        if sims:
            xs = sorted(sims)
            res.sim_p20 = xs[max(0, int(len(xs) * 0.2))]
        drop = (
            local_drop
            if local_drop is not None
            else resolve_local_drop(cfg, compute_local_drops(sims))
        )

        cuts, n_sem, n_hard = pick_cut_boundaries(
            sims,
            sents,
            local_tau,
            drop,
            int(cfg["chunk"]["hard_max_tokens"]),
            int(cfg["chunk"]["target_tokens"]),
            int(cfg["chunk"]["min_tokens"]),
        )
        res.n_cuts_semantic = n_sem
        res.n_cuts_hard = n_hard
        text, chunk_tokens, cut_stats = apply_cuts(
            sents,
            cuts,
            int(cfg["chunk"]["overlap_tokens"]),
            tok=tok,
            emb=emb,
            min_keep=int(cfg["chunk"].get("min_keep_tokens") or 50),
            hard_max=int(cfg["chunk"]["hard_max_tokens"]),
            cfg=cfg,
            doc_meta=doc_meta,
            tag=tag + " ",
        )
        out_path = out_root / path.relative_to(source)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")

        res.n_chunks = len(chunk_tokens)
        res.chunk_tokens = chunk_tokens
        res.n_enforce_splits = cut_stats.get("enforce_splits", 0)
        res.n_emergency_splits = cut_stats.get("emergency_splits", 0)
        res.n_ge_512 = cut_stats.get("n_ge_512", 0)
        if res.n_ge_512:
            res.error = "chunk_exceeds_512"
        res.elapsed_sec = time.time() - t0
        log(
            f"{tag} 完成 file={rel} chunks={res.n_chunks} "
            f"tok_min={min(chunk_tokens) if chunk_tokens else 0} "
            f"tok_max={max(chunk_tokens) if chunk_tokens else 0} "
            f"sem={n_sem} hard={n_hard} enforce={res.n_enforce_splits} "
            f"emerg={res.n_emergency_splits} ge512={res.n_ge_512} "
            f"tau={local_tau:.4f} cost={fmt_dur(res.elapsed_sec)} → {out_path}"
        )
        return res, sims
    except Exception as e:
        res.error = str(e)[:300]
        res.elapsed_sec = time.time() - t0
        log(f"{tag} ERROR file={rel} cost={fmt_dur(res.elapsed_sec)} {type(e).__name__}: {e}")
        return res, []


def split_output_chunks(text: str) -> list[str]:
    """把已写出的语义分块 MD 按独立一行的 ## 拆回块列表。"""
    text = text.replace("\r\n", "\n")
    return [p.strip() for p in re.split(r"\n##\s*\n", text) if p.strip()]


def fix_dir_merge(
    root: Path,
    tok: Tokenizer,
    emb: Embedder,
    cfg: dict[str, Any],
) -> dict[str, Any]:
    """后处理：小块合并 + enforce + 512 验收 + ## 清理。"""
    min_keep = int(cfg["chunk"].get("min_keep_tokens") or 50)
    hard_max = int(cfg["chunk"]["hard_max_tokens"])
    absolute_max = int(cfg["chunk"].get("absolute_max_tokens") or 512)
    files = sorted(root.rglob("*.md"))
    summary = {
        "n_files": 0,
        "merged": 0,
        "skipped": 0,
        "enforce_splits": 0,
        "emergency_splits": 0,
        "n_chunks_before": 0,
        "n_chunks_after": 0,
        "n_lt_min_keep": 0,
        "n_gt_hard_max": 0,
        "n_ge_512": 0,
        "double_hash_left": 0,
        "tokenizer_mode": tok.mode,
    }
    for path in files:
        raw = path.read_text(encoding="utf-8", errors="ignore")
        parts = split_output_chunks(raw)
        if not parts:
            continue
        summary["n_files"] += 1
        summary["n_chunks_before"] += len(parts)
        before = len(parts)
        parts, mstats = merge_small_chunks(parts, tok, emb, min_keep, hard_max)
        summary["merged"] += mstats["merged"]
        summary["skipped"] += mstats["skipped"]
        parts, fstats = finalize_parts(parts, tok, cfg, tag="[fix] ", emb=emb)
        summary["enforce_splits"] += fstats.get("enforce_splits", 0)
        summary["emergency_splits"] += fstats.get("emergency_splits", 0)
        text = join_chunks_with_sep(parts)
        path.write_text(text if text.endswith("\n") else text + "\n", encoding="utf-8")
        summary["n_chunks_after"] += len(parts)
        for p in parts:
            n = tok.count(p)
            if n < min_keep:
                summary["n_lt_min_keep"] += 1
            if n > hard_max:
                summary["n_gt_hard_max"] += 1
            if n >= absolute_max:
                summary["n_ge_512"] += 1
        if re.search(r"\n##\s*\n\s*##", text):
            summary["double_hash_left"] += 1
        log(
            f"[fix] {path.relative_to(root)} chunks {before}→{len(parts)} "
            f"merged={mstats['merged']} enforce={fstats.get('enforce_splits', 0)} "
            f"ge512={fstats.get('n_ge_512', 0)}"
        )
    return summary


def make_tokenizer(cfg: dict[str, Any]) -> Tokenizer:
    tc = cfg.get("tokenizer") or {}
    return Tokenizer(
        str(tc.get("name") or "BAAI/bge-large-zh-v1.5"),
        prefer_hf=bool(tc.get("prefer_hf")),
        local_path=str(tc.get("local_path") or ""),
    )


def write_report(
    report_dir: Path,
    results: list[ChunkResult],
    all_sims: list[float],
    tau: float,
    cfg: dict,
    local_drop: float | None = None,
) -> None:
    report_dir.mkdir(parents=True, exist_ok=True)
    n = len(results)
    csv_path = report_dir / f"trial_{n}_docs.csv"
    with csv_path.open("w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(
            f,
            fieldnames=[
                "rel_path",
                "bytes",
                "n_sents",
                "n_chunks",
                "min_tok",
                "p50_tok",
                "max_tok",
                "n_ge_512",
                "n_cuts_semantic",
                "n_cuts_hard",
                "n_enforce_splits",
                "n_emergency_splits",
                "tau",
                "elapsed_sec",
                "error",
            ],
        )
        w.writeheader()
        for r in results:
            toks = r.chunk_tokens or [0]
            st = sorted(toks)
            w.writerow(
                {
                    "rel_path": r.rel_path,
                    "bytes": r.bytes,
                    "n_sents": r.n_sents,
                    "n_chunks": r.n_chunks,
                    "min_tok": min(toks),
                    "p50_tok": st[len(st) // 2],
                    "max_tok": max(toks),
                    "n_ge_512": r.n_ge_512 or sum(1 for t in toks if t >= 512),
                    "n_cuts_semantic": r.n_cuts_semantic,
                    "n_cuts_hard": r.n_cuts_hard,
                    "n_enforce_splits": r.n_enforce_splits,
                    "n_emergency_splits": r.n_emergency_splits,
                    "tau": f"{r.tau:.4f}",
                    "elapsed_sec": f"{r.elapsed_sec:.1f}",
                    "error": r.error,
                }
            )

    all_toks = [t for r in results for t in r.chunk_tokens]
    all_toks_sorted = sorted(all_toks) if all_toks else [0]
    sim_sorted = sorted(all_sims) if all_sims else []

    def pct(xs: list[float] | list[int], p: float) -> float:
        if not xs:
            return 0.0
        k = max(0, min(len(xs) - 1, int(len(xs) * p / 100)))
        return float(xs[k])

    md = report_dir / f"trial_{n}_report.md"
    lines = [
        f"# 语义分块试验报告（{n} 篇）",
        "",
        f"- 源目录: `{cfg['source_root']}`",
        f"- 输出: `{cfg['output_root']}`",
        f"- 模型: `{cfg['embedding']['model']}` @ `{cfg['embedding']['base_url']}`",
        f"- workers: {cfg.get('embedding', {}).get('workers')}",
        f"- 全局 τ (P{cfg['threshold']['percentile']}): **{tau:.4f}**",
        (
            f"- local_drop（动态标定）: **{local_drop:.4f}**"
            if local_drop is not None
            else f"- local_drop: {cfg['threshold'].get('local_drop')}"
        ),
        f"- 文档数: {len(results)}；失败: {sum(1 for r in results if r.error)}",
        f"- 总块数: {len(all_toks)}",
        f"- 块 token: min={min(all_toks_sorted)} / P50={pct(all_toks_sorted,50):.0f} / P90={pct(all_toks_sorted,90):.0f} / max={max(all_toks_sorted)}",
        f"- ≥512 token 的块: **{sum(1 for t in all_toks if t >= 512)}**（目标 0）",
        f"- ≥硬顶({cfg['chunk']['hard_max_tokens']}) 的块: {sum(1 for t in all_toks if t >= cfg['chunk']['hard_max_tokens'])}",
        f"- enforce 切刀总数: {sum(r.n_enforce_splits for r in results)}",
        f"- emergency 切刀总数: {sum(r.n_emergency_splits for r in results)}",
        f"- 100–{cfg['chunk']['hard_max_tokens']} tok 占比: {100*sum(1 for t in all_toks if 100<=t<=cfg['chunk']['hard_max_tokens'])/max(1,len(all_toks)):.1f}%",
        f"- 邻窗 sim: P10={pct(sim_sorted,10):.4f} P20={pct(sim_sorted,20):.4f} P50={pct(sim_sorted,50):.4f} P90={pct(sim_sorted,90):.4f}",
        "",
        "## 结论要点",
        "",
        "- 阈值未拍脑袋定 0.7，由本批邻窗相似度分位数标定。",
        "- 成品经 enforce + 模型 tokenizer 512 验收；入库仍用 separator=`##` + max_tokens=512。",
        "",
        f"明细 CSV: `{csv_path.name}`",
    ]
    md.write_text("\n".join(lines) + "\n", encoding="utf-8")

    meta = {
        "tau": tau,
        "n_docs": len(results),
        "n_chunks": len(all_toks),
        "sim_percentiles": {f"p{p}": pct(sim_sorted, p) for p in (10, 20, 50, 75, 90)},
        "chunk_token_percentiles": {f"p{p}": pct(all_toks_sorted, p) for p in (10, 50, 90)},
        "n_ge_512": sum(1 for t in all_toks if t >= 512),
    }
    (report_dir / f"trial_{n}_meta.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    log(f"[report] {md}")


def main() -> int:
    # 无缓冲，保证 tee / nohup 实时
    try:
        sys.stdout.reconfigure(line_buffering=True)  # type: ignore[attr-defined]
    except Exception:
        pass

    ap = argparse.ArgumentParser(
        description="滑动窗口语义分块：可抽样试验，或传入 md 文件/目录绝对路径全量切分"
    )
    ap.add_argument(
        "input_path",
        nargs="?",
        default="",
        help="要处理的 .md 文件或目录绝对路径（目录则递归全部 md；省略则按配置抽样）",
    )
    ap.add_argument("--config", default=str(DEFAULT_CFG))
    ap.add_argument("--n", type=int, default=0, help="覆盖配置中的抽样数（无 input_path 时）")
    ap.add_argument("--out", default="", help="输出目录（有 input_path 时：默认 输入名_semantic）")
    ap.add_argument(
        "--workers",
        type=int,
        default=0,
        help="文档级并发数（覆盖配置 embedding.workers；0=用配置）",
    )
    ap.add_argument("--all-sims-first", action="store_true", help="先扫完全部邻窗再统一 τ（默认）")
    ap.add_argument(
        "--fix-dir",
        default="",
        help="仅后处理已有语义分块 MD：向量择邻合并碎块 + 清理多余 ##（不重跑切分）",
    )
    args = ap.parse_args()

    cfg = yaml.safe_load(Path(args.config).read_text(encoding="utf-8"))
    source = Path(cfg.get("source_root") or ".")
    out_root = Path(cfg.get("output_root") or "./out_semantic")
    report_dir = Path(cfg.get("report_dir") or (str(out_root) + "/_reports"))

    run_paths: list[Path] | None = None
    if args.input_path:
        inp = Path(args.input_path).expanduser().resolve()
        if not inp.exists():
            log(f"[error] 路径不存在: {inp}")
            return 1
        if inp.is_file():
            if inp.suffix.lower() != ".md":
                log(f"[error] 只支持 .md 文件: {inp}")
                return 1
            run_paths = [inp]
            source = inp.parent
            out_root = (
                Path(args.out).expanduser().resolve()
                if args.out
                else Path(str(source) + "_semantic")
            )
        else:
            run_paths = sorted(inp.rglob("*.md"))
            source = inp
            out_root = (
                Path(args.out).expanduser().resolve()
                if args.out
                else Path(str(inp) + "_semantic")
            )
        report_dir = out_root / "_reports"
        if not run_paths:
            log(f"[error] 未找到任何 .md: {inp}")
            return 1
        log(f"[input] {inp} 共 {len(run_paths)} 个 md → 输出 {out_root}")

    if args.workers and args.workers > 0:
        cfg.setdefault("embedding", {})["workers"] = int(args.workers)

    if args.fix_dir:
        fix_root = Path(args.fix_dir)
        if not fix_root.is_dir():
            log(f"[fix] 目录不存在: {fix_root}")
            return 1
        pace = Pace()
        tok = make_tokenizer(cfg)
        emb = Embedder(
            cfg["embedding"]["base_url"],
            cfg["embedding"]["model"],
            int(cfg["embedding"]["batch_size"]),
            int(cfg["embedding"]["timeout_sec"]),
            pace=pace,
            tok=tok,
            max_input_tokens=min(504, int(cfg["chunk"].get("absolute_max_tokens") or 512) - 8),
            max_batch_chars=int(cfg["embedding"].get("max_batch_chars") or 6000),
        )
        probe_embedding_rate(emb, n=min(16, int(cfg["embedding"]["batch_size"])))
        log(
            f"[fix] start dir={fix_root} tokenizer={tok.mode} "
            f"hard_max={cfg['chunk']['hard_max_tokens']} "
            f"absolute_max={cfg['chunk'].get('absolute_max_tokens', 512)}"
        )
        summary = fix_dir_merge(fix_root, tok, emb, cfg)
        report_dir.mkdir(parents=True, exist_ok=True)
        fix_report = report_dir / "fix_merge_report.json"
        fix_report.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        log(f"[fix] done {json.dumps(summary, ensure_ascii=False)}")
        log(f"[fix] report → {fix_report}")
        return 0

    out_root.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)

    if run_paths is not None:
        paths = run_paths
        quota = {"_all": len(paths)}
    else:
        quota = dict(cfg.get("sample", {}).get("quota") or {})
        sample_n = int(cfg.get("sample", {}).get("n") or 0)
        if args.n and args.n != sample_n:
            total = sum(quota.values()) or 1
            scale = args.n / total
            quota = {k: max(1, int(v * scale)) for k, v in quota.items()}
            while sum(quota.values()) > args.n:
                k = max(quota, key=quota.get)
                if quota[k] > 1:
                    quota[k] -= 1
                else:
                    break
            while sum(quota.values()) < args.n:
                k = max(quota, key=quota.get)
                quota[k] += 1
        paths = pick_sample(source, quota, int(cfg.get("sample", {}).get("seed") or 42))

    # 路径 → 稳定序号（1-based），并发时日志也能对上
    path_index = {p: i + 1 for i, p in enumerate(paths)}

    pace = Pace()
    tok = make_tokenizer(cfg)
    emb = Embedder(
        cfg["embedding"]["base_url"],
        cfg["embedding"]["model"],
        int(cfg["embedding"]["batch_size"]),
        int(cfg["embedding"]["timeout_sec"]),
        pace=pace,
        tok=tok,
        max_input_tokens=min(504, int(cfg["chunk"].get("absolute_max_tokens") or 512) - 8),
        max_batch_chars=int(cfg["embedding"].get("max_batch_chars") or 6000),
    )
    workers = max(1, int(cfg.get("embedding", {}).get("workers") or 4))

    log("=" * 72)
    log(f"[start] semantic_chunk n={len(paths)} workers={workers} batch={cfg['embedding']['batch_size']}")
    log(f"[start] source={source}")
    log(f"[start] output={out_root}")
    log(f"[start] report={report_dir}")
    log(f"[start] model={cfg['embedding']['model']} @ {cfg['embedding']['base_url']}")
    for cat, n in quota.items():
        log(f"[sample] quota {cat}: {n}")

    # 先实测 embedding，再出 ETA（禁止固定假设吞吐）
    probe_n = min(32, int(cfg["embedding"]["batch_size"]))
    probe_embedding_rate(emb, n=probe_n)
    rate0 = pace.rate()
    if rate0 is None:
        log("[plan] 警告：probe 未拿到吞吐，文档 ETA 暂显示 ?，首批 embed 后自动修正")
    else:
        log(f"[plan] 当前实测吞吐 {rate0:.2f} txt/s（墙钟，随每批 embed 滚动更新）")

    total_bytes = 0
    total_seams_est = 0
    plan_rows: list[tuple[int, int, int, Path]] = []  # idx, bytes, seams_est, path
    for p in paths:
        raw = p.read_text(encoding="utf-8", errors="ignore")
        b = len(raw.encode("utf-8", errors="ignore"))
        rough_sents = max(1, len([x for x in SENT_SPLIT.split(raw) if x.strip()]))
        seams = max(0, rough_sents - 1)
        total_bytes += b
        total_seams_est += seams
        plan_rows.append((path_index[p], b, seams, p))

    total_texts_est = total_seams_est * 2
    # pass1 全量 embed；pass2 多数 cache，按实测吞吐估 15% 额外墙钟
    eta_pass1 = pace.eta(total_texts_est)
    eta_pass2 = pace.eta(int(total_texts_est * 0.15))
    eta_all = None if eta_pass1 is None else eta_pass1 + (eta_pass2 or 0)
    log(
        f"[plan] docs={len(paths)} total_size={fmt_size(total_bytes)} "
        f"rough_seams≈{total_seams_est} embed_texts≈{total_texts_est} "
        f"(pass1为主) rate={f'{rate0:.2f}txt/s' if rate0 else '?'} "
        f"ETA≈{fmt_eta(eta_all)}"
    )
    log("[plan] 文档清单（按实测吞吐估 ETA；大文件优先拖时间）:")
    for idx, b, seams, p in sorted(plan_rows, key=lambda x: -x[1]):
        rel = str(p.relative_to(source)).replace("\\", "/")
        texts = seams * 2
        doc_eta = pace.eta(texts)
        log(
            f"  #{idx:02d}/{len(paths)} size={fmt_size(b):>8} "
            f"seams≈{seams:<4} texts≈{texts:<4} ETA≈{fmt_eta(doc_eta):>6}  {rel}"
        )

    # Pass 1
    log("-" * 72)
    log("[pass1] 开始：收集邻窗相似度，标定全局 τ 与 local_drop")
    all_sims: list[float] = []
    all_drops: list[float] = []
    sims_lock = threading.Lock()
    done_n = 0
    remaining_texts = {"n": total_texts_est}
    rem_lock = threading.Lock()

    def collect_one(p: Path) -> tuple[Path, list[float], int, int, float]:
        idx = path_index[p]
        tag = f"[pass1 {idx}/{len(paths)}]"
        rel = str(p.relative_to(source)).replace("\\", "/")
        t0 = time.time()
        try:
            raw = p.read_text(encoding="utf-8", errors="ignore")
            b = len(raw.encode("utf-8", errors="ignore"))
            body, _, _ = preprocess_metadata(raw, cfg)
            hard_max = int(cfg.get("chunk", {}).get("hard_max_tokens") or 495)
            sents = split_sentences(body, tok, cfg)
            sents = pre_split_oversized_sentences(
                sents, tok, hard_max, overlap=0, tag=tag.strip()
            )
            seams, n_texts, n_batches = estimate_doc_work(len(sents), emb.batch_size)
            eta_doc = pace.eta(n_texts)
            rate_now = pace.rate()
            log(
                f"{tag} 开始 file={rel} size={fmt_size(b)} "
                f"sents={len(sents)} seams={seams} embed_texts≈{n_texts} "
                f"batches≈{n_batches} rate={f'{rate_now:.1f}txt/s' if rate_now else '?'} "
                f"ETA≈{fmt_eta(eta_doc)}"
            )
            if len(sents) < 2:
                log(f"{tag} 跳过(≤1句) cost={fmt_dur(time.time()-t0)}")
                return p, [], b, 0, time.time() - t0
            sims = neighbor_sims(sents, emb, cfg["window"], doc_tag=tag)
            dt = time.time() - t0
            log(
                f"{tag} 完成 file={rel} seams={len(sims)} "
                f"cost={fmt_dur(dt)} rate={(n_texts/dt) if dt>0 else 0:.1f}txt/s"
            )
            return p, sims, b, n_texts, dt
        except Exception as e:
            log(f"{tag} ERROR file={rel} {type(e).__name__}: {e}")
            return p, [], 0, 0, time.time() - t0

    t0 = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(collect_one, p): p for p in paths}
        for fut in as_completed(futs):
            try:
                _p, sims, _b, n_texts, _dt = fut.result()
            except Exception as e:
                log(f"[pass1] worker FAIL {type(e).__name__}: {e}")
                with sims_lock:
                    done_n += 1
                continue
            with sims_lock:
                all_sims.extend(sims)
                if sims:
                    all_drops.extend(compute_local_drops(sims))
                done_n += 1
            with rem_lock:
                remaining_texts["n"] = max(0, remaining_texts["n"] - n_texts)
                rem = remaining_texts["n"]
            done_txt, wall_rate, elapsed, batch_ewma = pace.snapshot()
            rate_s = f"{wall_rate:.1f}" if wall_rate else "?"
            ewma_s = f"{batch_ewma:.1f}" if batch_ewma else "?"
            log(
                f"[pass1] 进度 {done_n}/{len(paths)} docs | "
                f"emb_calls={emb.n_calls} texts={emb.n_texts} cache={len(emb.cache)} | "
                f"墙钟吞吐={rate_s}txt/s 单批EWMA={ewma_s}txt/s "
                f"elapsed={fmt_dur(elapsed)} "
                f"ETA_remain≈{fmt_eta(pace.eta(rem + int(rem * 0.15)))}"
            )

    tau = calibrate_tau(all_sims, float(cfg["threshold"]["percentile"]))
    local_drop = resolve_local_drop(cfg, all_drops)
    log(
        f"[calibrate] n_sims={len(all_sims)} n_drops={len(all_drops)} "
        f"tau_P{cfg['threshold']['percentile']}={tau:.4f} "
        f"local_drop={local_drop:.4f} "
        f"(mode={cfg['threshold'].get('local_drop_mode', 'percentile')} "
        f"P{cfg['threshold'].get('local_drop_percentile', 80)}) "
        f"pass1_cost={fmt_dur(time.time()-t0)}"
    )
    if all_sims:
        xs = sorted(all_sims)
        for pct in (10, 20, 50, 90):
            log(f"[calibrate] sim P{pct}={xs[int(len(xs)*pct/100)]:.4f}")
    if all_drops:
        ds = sorted(all_drops)
        for pct in (50, 80, 90):
            log(f"[calibrate] drop P{pct}={ds[int(len(ds)*pct/100)]:.4f}")

    # Pass 2（cache 命中为主）
    log("-" * 72)
    log(f"[pass2] 开始：按 τ={tau:.4f} local_drop={local_drop:.4f} 切分并写 MD")
    results: list[ChunkResult] = []
    done_n = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {
            pool.submit(
                process_one,
                p,
                source,
                out_root,
                tok,
                emb,
                cfg,
                tau,
                path_index[p],
                len(paths),
                "pass2",
                local_drop,
            ): p
            for p in paths
        }
        for fut in as_completed(futs):
            res, _ = fut.result()
            results.append(res)
            done_n += 1
            bad = sum(1 for r in results if r.error)
            log(
                f"[pass2] 进度 {done_n}/{len(paths)} written errors={bad} "
                f"elapsed={fmt_dur(time.time()-t0)}"
            )

    results.sort(key=lambda r: r.rel_path)
    write_report(report_dir, results, all_sims, tau, cfg, local_drop=local_drop)
    done_txt, wall_rate, _, batch_ewma = pace.snapshot()
    rate_s = f"{wall_rate:.1f}" if wall_rate else "?"
    ewma_s = f"{batch_ewma:.1f}" if batch_ewma else "?"
    log(
        f"[done] docs={len(results)} emb_calls={emb.n_calls} texts={emb.n_texts} "
        f"cache={len(emb.cache)} 墙钟吞吐={rate_s}txt/s 单批EWMA={ewma_s}txt/s "
        f"total={fmt_dur(time.time()-t0)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
