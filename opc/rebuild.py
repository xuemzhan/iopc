"""从 Obsidian 库重建 SQLite 索引：让「删库可恢复」成立。

只读 vault，先在临时库构建，成功后原子替换 `data/opc.db`。
重建内容：sources / documents / chunks / notes / entities / concepts / edges。
"""
from __future__ import annotations

import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

from .cooccur import rebuild_for_source
from .db import Database
from .util import normalize_key, now_iso, slugify
from .vault import Vault

LINK_RE = re.compile(r"\[\[([^\]|#]+)(?:[|#][^\]]*)?\]\]")


def _split_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            try:
                fm = yaml.safe_load(parts[1]) or {}
                return (fm if isinstance(fm, dict) else {}), parts[2].lstrip("\n")
            except Exception:
                return {}, text
    return {}, text


def _body_after_h1(body: str) -> tuple[str, str]:
    """返回 (标题, 正文)。"""
    lines = body.splitlines()
    if lines and lines[0].startswith("# "):
        return lines[0][2:].strip(), "\n".join(lines[1:]).strip()
    return "", body.strip()


def _summary_of(body: str, limit: int = 240) -> str:
    for para in body.split("\n"):
        p = para.strip()
        if p and not p.startswith(("#", "-", ">", "|", "```")):
            return p[:limit]
    return ""


def _rel(vault: Vault, p: Path) -> str:
    return p.relative_to(vault.root).as_posix()


def build_from_vault(vault: Vault | None = None, db_path: Path | None = None) -> dict[str, Any]:
    vault = vault or Vault()
    db_path = db_path or (vault.root.parent / "opc-rebuild.db")
    if db_path.exists():
        db_path.unlink()
    database = Database(db_path)

    # 1) 实体 / 概念
    entity_ids: dict[str, int] = {}
    concept_ids: dict[str, int] = {}
    related_links: list[tuple[str, str, str]] = []  # (kind, slug, target_link)
    for kind, folder, table in (("entity", "entities", "entities"),
                                ("concept", "concepts", "concepts")):
        for p in sorted((vault.wiki / folder).glob("*.md")):
            fm, raw = _split_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
            title, body = _body_after_h1(raw)
            name = str(fm.get("title") or title or p.stem)
            aliases = fm.get("aliases") or []
            if isinstance(aliases, str):
                aliases = [aliases]
            summary = _summary_of(body)
            key = normalize_key(name)
            if kind == "entity":
                etype = str(fm.get("entity_type") or (fm.get("tags") or ["other"])[0])
                eid = database.upsert_entity(key, name, etype, aliases=[str(a) for a in aliases],
                                             summary=summary, page_path=_rel(vault, p))
                entity_ids[p.stem] = eid
                entity_ids[slugify(name)] = eid
            else:
                cid = database.upsert_concept(key, name, summary,
                                              aliases=[str(a) for a in aliases],
                                              page_path=_rel(vault, p))
                concept_ids[p.stem] = cid
                concept_ids[slugify(name)] = cid
            for target in LINK_RE.findall(body):
                related_links.append((kind, p.stem, target.strip()))

    # 2) 来源页
    source_rows: list[dict[str, Any]] = []
    for p in sorted((vault.wiki / "sources").glob("*.md")):
        fm, raw = _split_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
        title, body = _body_after_h1(raw)
        title = str(fm.get("title") or title or p.stem)
        kind = str(fm.get("kind") or "note")
        refs = fm.get("sources") or []
        if isinstance(refs, str):
            refs = [refs]
        uri = str(refs[0]) if refs else None
        sid = database.upsert_source(kind, p.stem, title, uri=uri,
                                     content_hash=None, raw_path=None,
                                     meta={"rebuild": True, "page": _rel(vault, p)})
        database.put_document(sid, body)
        database.put_chunks(sid, sid, [body])
        database.put_note(source_id=sid, kind="source", title=title, slug=p.stem,
                          path=_rel(vault, p), summary=_summary_of(body), body_md=body,
                          tags=[str(t) for t in (fm.get("tags") or [])],
                          status="published", model="rebuild")
        source_rows.append({"id": sid, "body": body, "slug": p.stem, "title": title})

    # 3) synthetic 页（日报/综述/体检/行动项），只入 notes 表
    synthetic_dir = vault.wiki / "synthetic"
    if synthetic_dir.exists():
        for p in sorted(synthetic_dir.glob("*.md")):
            fm, raw = _split_frontmatter(p.read_text(encoding="utf-8", errors="ignore"))
            title, body = _body_after_h1(raw)
            database.put_note(source_id=None, kind="synthetic",
                              title=str(fm.get("title") or title or p.stem), slug=p.stem,
                              path=_rel(vault, p), summary=_summary_of(body), body_md=body,
                              tags=[str(t) for t in (fm.get("tags") or [])],
                              status="published", model="rebuild")

    # 4) 边：来源页里的实体/概念链接
    for row in source_rows:
        for target in LINK_RE.findall(row["body"]):
            t = target.strip()
            slug = t.split("/")[-1]
            if t.startswith("entities/") and slug in entity_ids:
                database.add_edge("source", row["id"], "entity", entity_ids[slug],
                                  "mentions", weight=1.0, source_id=row["id"])
            elif t.startswith("concepts/") and slug in concept_ids:
                database.add_edge("source", row["id"], "concept", concept_ids[slug],
                                  "covers", weight=1.0, source_id=row["id"])

    # 5) 实体/概念页里的 Related 链接
    for kind, slug, target in related_links:
        t = target.split("/")[-1]
        if t in entity_ids and slug in entity_ids:
            database.add_edge(kind, entity_ids[slug], "entity", entity_ids[t], "related_to",
                              weight=1.0)
        elif t in concept_ids and slug in concept_ids:
            database.add_edge(kind, concept_ids[slug], "concept", concept_ids[t], "related_to",
                              weight=1.0)

    # 6) 句级共现（用来源页正文 + 已建好的实体）
    for row in source_rows:
        ents = []
        with database.connect() as c:
            for r in c.execute(
                "SELECT e.id, e.name, e.aliases FROM edges g JOIN entities e ON e.id=g.dst_id"
                " WHERE g.src_type='source' AND g.src_id=? AND g.dst_type='entity'",
                (row["id"],),
            ):
                import json

                try:
                    aliases = json.loads(r["aliases"] or "[]")
                except Exception:
                    aliases = []
                ents.append({"id": int(r["id"]), "name": r["name"], "aliases": aliases})
        if len(ents) >= 2:
            rebuild_for_source(database, row["id"], row["body"], ents)

    with database.connect() as c:
        counts = {
            "sources": c.execute("SELECT COUNT(*) FROM sources").fetchone()[0],
            "notes": c.execute("SELECT COUNT(*) FROM notes").fetchone()[0],
            "entities": c.execute("SELECT COUNT(*) FROM entities").fetchone()[0],
            "concepts": c.execute("SELECT COUNT(*) FROM concepts").fetchone()[0],
            "edges": c.execute("SELECT COUNT(*) FROM edges").fetchone()[0],
        }
    return {"db": str(db_path), "counts": counts}


def _hub_alive() -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen("http://127.0.0.1:5370/api/health", timeout=1) as r:
            return r.status == 200
    except Exception:
        return False


def swap_in(db_path: Path, backup_dir: Path | None = None) -> str:
    """把重建好的库替换为正式库：先 checkpoint，再复制备份，最后原子替换。

    顺序保证：目标库在任何时刻都存在（复制而非移动），WAL 里的事务不会被丢掉。
    """
    import sqlite3

    backup_dir = backup_dir or db_path.parent
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_dir / f"opc.db.bak-{stamp}"

    if db_path.exists():
        # 把 WAL 落盘，保证备份与替换后的库都包含最新事务
        try:
            con = sqlite3.connect(str(db_path))
            con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            con.close()
        except Exception:
            pass
        shutil.copy2(str(db_path), str(backup))

    tmp = db_path.parent / "opc-rebuild.db"
    os.replace(str(tmp), str(db_path))  # 原子替换，目标不会出现空窗
    for suffix in ("-wal", "-shm"):
        side = Path(str(db_path) + suffix)
        if side.exists():
            side.unlink()
    return str(backup)


def _build_in_subprocess(tmp: Path, vault_root: Path) -> None:
    """在独立进程里建库：进程退出即释放所有 sqlite 句柄，父进程才能原子替换。"""
    import subprocess
    import sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent)
    cmd = [sys.executable, "-m", "opc.rebuild", "--build", str(tmp),
           "--vault", str(vault_root)]
    proc = subprocess.run(cmd, capture_output=True, env=env)
    if proc.returncode != 0:
        raise RuntimeError(
            f"重建子进程失败：{proc.stderr.decode('utf-8', 'ignore')[-800:]}"
        )


def rebuild(*, from_vault: bool = False, database_path: Path | None = None,
            vault: Vault | None = None, force: bool = False) -> dict[str, Any]:
    if not from_vault:
        raise ValueError("目前只支持 --from-vault")
    if _hub_alive() and not force:
        raise RuntimeError("Hub 正在运行，重建会替换数据库；先停 Hub，或加 --force")
    vault = vault or Vault()
    target = Path(database_path) if database_path else cfg_db_path()
    tmp = target.parent / "opc-rebuild.db"
    _build_in_subprocess(tmp, vault.root)
    import time

    time.sleep(0.3)
    result = {"db": str(tmp), "counts": _counts(tmp)}
    backup = swap_in(target)
    result["backup"] = backup
    result["swapped_to"] = str(target)
    result["at"] = now_iso()
    return result


def cfg_db_path() -> Path:
    from .config import cfg

    return cfg().db_path


def _counts(path: Path) -> dict[str, int]:
    import sqlite3

    c = sqlite3.connect(str(path))
    try:
        return {
            name: c.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0]
            for name in ("sources", "notes", "entities", "concepts", "edges")
        }
    finally:
        c.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--build", required=True)
    parser.add_argument("--vault", default="")
    args = parser.parse_args()
    v = Vault(Path(args.vault)) if args.vault else Vault()
    info = build_from_vault(v, Path(args.build))
    print(info["counts"])
