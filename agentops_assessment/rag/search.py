from __future__ import annotations

import math
import re
from collections import Counter
from typing import Any

from agentops_assessment.backend import database


def tokenize(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9-]+|[一-鿿]", text.lower())


def cosine_score(query_tokens: list[str], doc_tokens: list[str]) -> float:
    if not query_tokens or not doc_tokens:
        return 0.0
    q = Counter(query_tokens)
    d = Counter(doc_tokens)
    dot = sum(q[token] * d[token] for token in q.keys() & d.keys())
    q_norm = math.sqrt(sum(v * v for v in q.values()))
    d_norm = math.sqrt(sum(v * v for v in d.values()))
    if not q_norm or not d_norm:
        return 0.0
    return dot / (q_norm * d_norm)


class KnowledgeIndex:
    """轻量级本地检索索引。

    完成权限感知检索、重排、答案生成、引用溯源
    和被过滤文档报告。文档正文视为不可信数据，不能让正文中的
    指令改变系统策略；完成实现后不得向 API 返回 debug/candidate_note。
    """

    def search(
        self,
        query: str,
        user_permissions: list[str],
        top_k: int = 3,
    ) -> dict[str, Any]:
        with database.connect() as conn:
            database.init_db(conn)
            rows = conn.execute(
                """
                SELECT id, doc_id, source_path, title, permission, content
                FROM knowledge_chunks
                """
            ).fetchall()

        query_tokens = tokenize(query)

        visible: list[dict[str, Any]] = []
        filtered_doc_ids: set[str] = set()

        for row in rows:
            # 权限检查：用户有对应权限才可以看
            # permission 为空或为 knowledge:read 时默认可见
            perm = row["permission"]
            if perm and perm not in user_permissions and perm != "knowledge:read":
                filtered_doc_ids.add(row["doc_id"])
                continue

            content = row["content"]
            doc_tokens = tokenize(content)
            score = cosine_score(query_tokens, doc_tokens)

            visible.append({
                "chunk_id": row["id"],
                "doc_id": row["doc_id"],
                "title": row["title"],
                "source_path": row["source_path"],
                "content": content,
                "score": score,
            })

        # 按相关性排序
        visible.sort(key=lambda x: x["score"], reverse=True)
        top = visible[:top_k]

        # 构建引用（可追溯来源）
        citations = [
            {
                "doc_id": chunk["doc_id"],
                "title": chunk["title"],
                "source_path": chunk["source_path"],
                "chunk_id": chunk["chunk_id"],
            }
            for chunk in top
        ]

        # 生成简短回答（基于可见 chunk 内容）
        if top:
            snippet_parts: list[str] = []
            for chunk in top:
                # 只取每段前 120 字作为摘要
                text = chunk["content"][:120].strip().replace("\n", " ")
                if text:
                    snippet_parts.append(f"根据 {chunk['title']}：{text}")
            answer = "；".join(snippet_parts) if snippet_parts else ""
        else:
            answer = ""

        return {
            "answer": answer,
            "citations": citations,
            "filtered_doc_ids": sorted(filtered_doc_ids),
        }
