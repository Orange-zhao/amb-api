"""
AMB Search API — PostgreSQL version for Railway deployment
===========================================================
Reads DATABASE_URL from environment variable (set by Railway automatically).
Falls back to SQLite for local development.

Run locally:
    pip install fastapi uvicorn psycopg2-binary python-dotenv
    python amb_api.py
"""

import os
import re
from typing import Optional
from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware

# ── Database connection ───────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL")  # Set automatically by Railway

if DATABASE_URL:
    # PostgreSQL mode (Railway)
    import psycopg2
    import psycopg2.extras

    # Railway sometimes provides postgres:// but psycopg2 needs postgresql://
    if DATABASE_URL.startswith("postgres://"):
        DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

    def get_db():
        conn = psycopg2.connect(DATABASE_URL)
        return conn

    def fetchall(cur):
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def fetchone(cur):
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row)) if row else None

    PLACEHOLDER = "%s"
    print("Running in PostgreSQL mode")

else:
    # SQLite mode (local development)
    import sqlite3
    DB_PATH = r"D:\Asian medicines\amb.db"

    def get_db():
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        return conn

    def fetchall(cur):
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in cur.fetchall()]

    def fetchone(cur):
        columns = [desc[0] for desc in cur.description]
        row = cur.fetchone()
        return dict(zip(columns, row)) if row else None

    PLACEHOLDER = "?"
    print("Running in SQLite mode (local)")


# ── App setup ─────────────────────────────────────────────────────────────────
app = FastAPI(title="AMB Search API", version="2.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET"],
    allow_headers=["*"],
)


# ── 1. Search ─────────────────────────────────────────────────────────────────
@app.get("/search")
def search(
    q:        Optional[str] = Query(None),
    tag:      Optional[str] = Query(None),
    tag_type: Optional[str] = Query(None),
    year_min: Optional[int] = Query(None),
    year_max: Optional[int] = Query(None),
    limit:    int            = Query(50),
    offset:   int            = Query(0),
):
    conn = get_db()
    cur  = conn.cursor()

    where  = ["1=1"]
    params = []
    p      = PLACEHOLDER

    if q:
        if DATABASE_URL:
            where.append(f"(r.title ILIKE {p} OR r.abstract ILIKE {p} OR r.author ILIKE {p})")
        else:
            where.append(f"(r.title LIKE {p} OR r.abstract LIKE {p} OR r.author LIKE {p})")
        like = f"%{q}%"
        params += [like, like, like]

    if tag:
        where.append(f"""
            r.key IN (
                SELECT rt.key FROM ref_tags rt
                JOIN tags t ON rt.tag_id = t.tag_id
                WHERE t.tag_name = {p}
            )
        """)
        params.append(tag)

    if tag_type:
        where.append(f"""
            r.key IN (
                SELECT rt.key FROM ref_tags rt
                JOIN tags t ON rt.tag_id = t.tag_id
                WHERE t.tag_type = {p}
            )
        """)
        params.append(tag_type)

    if year_min:
        where.append(f"r.pub_year >= {p}")
        params.append(year_min)

    if year_max:
        where.append(f"r.pub_year <= {p}")
        params.append(year_max)

    where_clause = " AND ".join(where)

    count_sql = f"""
        SELECT COUNT(DISTINCT r.key)
        FROM ref_records r
        LEFT JOIN ref_tags rt ON r.key = rt.key
        LEFT JOIN tags t ON rt.tag_id = t.tag_id
        WHERE {where_clause}
    """
    cur.execute(count_sql, params)
    total = cur.fetchone()[0]

    if DATABASE_URL:
        agg = "STRING_AGG(t.tag_name, '|')"
        nulls = "NULLS LAST"
    else:
        agg = "GROUP_CONCAT(t.tag_name, '|')"
        nulls = "NULLS LAST"

    sql = f"""
        SELECT r.key, r.title, r.author, r.pub_year, r.doi, r.url,
               r.publication, r.abstract, r.source_db,
               {agg} as tags
        FROM ref_records r
        LEFT JOIN ref_tags rt ON r.key = rt.key
        LEFT JOIN tags t ON rt.tag_id = t.tag_id
        WHERE {where_clause}
        GROUP BY r.key, r.title, r.author, r.pub_year, r.doi,
                 r.url, r.publication, r.abstract, r.source_db
        ORDER BY r.pub_year DESC {nulls}
        LIMIT {p} OFFSET {p}
    """
    cur.execute(sql, params + [limit, offset])
    rows = fetchall(cur)
    conn.close()

    return {
        "total":   total,
        "offset":  offset,
        "limit":   limit,
        "results": [
            {
                "key":         r["key"],
                "title":       r["title"],
                "author":      r["author"],
                "year":        r["pub_year"],
                "doi":         r["doi"],
                "url":         r["url"],
                "publication": r["publication"],
                "abstract":    r["abstract"],
                "source_db":   r["source_db"],
                "tags":        r["tags"].split("|") if r.get("tags") else [],
            }
            for r in rows
        ]
    }


# ── 2. Tag tree ───────────────────────────────────────────────────────────────
@app.get("/tag-tree")
def get_tag_tree():
    conn = get_db()
    cur  = conn.cursor()

    if DATABASE_URL:
        cur.execute("""
            SELECT t.tag_id, t.tag_name, t.level, t.parent_tag_id, t.source,
                   COUNT(rt.key) as record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            GROUP BY t.tag_id, t.tag_name, t.level, t.parent_tag_id, t.source
            ORDER BY t.level, t.tag_id
        """)
    else:
        cur.execute("""
            SELECT t.tag_id, t.tag_name, t.level, t.parent_tag_id, t.source,
                   COUNT(rt.key) as record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            GROUP BY t.tag_id
            ORDER BY t.level, t.tag_id
        """)

    rows = fetchall(cur)
    conn.close()

    nodes = {}
    for r in rows:
        nodes[r["tag_id"]] = {
            "id":       r["tag_id"],
            "name":     r["tag_name"],
            "level":    r["level"],
            "source":   r["source"],
            "count":    r["record_count"],
            "children": []
        }

    roots = []
    for r in rows:
        node = nodes[r["tag_id"]]
        if r["parent_tag_id"] and r["parent_tag_id"] in nodes:
            nodes[r["parent_tag_id"]]["children"].append(node)
        elif r["parent_tag_id"] is None:
            roots.append(node)

    return {"tree": roots}


# ── 3. Flat tags (for legacy sidebar) ────────────────────────────────────────
@app.get("/tags")
def get_tags():
    conn = get_db()
    cur  = conn.cursor()
    cur.execute("""
        SELECT t.tag_name, t.tag_type, COUNT(rt.key) as count
        FROM tags t
        LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
        WHERE t.tag_type IS NOT NULL
        GROUP BY t.tag_id, t.tag_name, t.tag_type
        ORDER BY t.tag_type, count DESC
    """)
    rows = fetchall(cur)
    conn.close()

    result = {"method": [], "topic": [], "region": []}
    for r in rows:
        ttype = r.get("tag_type")
        if ttype in result:
            result[ttype].append({"name": r["tag_name"], "count": r["count"]})
    return result


# ── 4. Single record ──────────────────────────────────────────────────────────
@app.get("/record/{key}")
def get_record(key: str):
    conn = get_db()
    cur  = conn.cursor()
    p    = PLACEHOLDER

    cur.execute(f"SELECT * FROM ref_records WHERE key = {p}", [key])
    r = fetchone(cur)
    if not r:
        return {"error": "not found"}

    cur.execute(f"""
        SELECT t.tag_name, t.tag_type, t.level FROM ref_tags rt
        JOIN tags t ON rt.tag_id = t.tag_id
        WHERE rt.key = {p}
    """, [key])
    tags = fetchall(cur)
    conn.close()

    return {
        **r,
        "tags": [{"name": t["tag_name"], "type": t.get("tag_type"), "level": t.get("level")}
                 for t in tags]
    }


# ── 5. Stats ──────────────────────────────────────────────────────────────────
@app.get("/stats")
def get_stats():
    conn = get_db()
    cur  = conn.cursor()

    cur.execute("SELECT COUNT(*) FROM ref_records")
    total = cur.fetchone()[0]

    cur.execute("SELECT COUNT(DISTINCT key) FROM ref_tags")
    tagged = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM ref_records WHERE doi IS NULL OR doi=''")
    no_doi = cur.fetchone()[0]

    cur.execute("SELECT COUNT(*) FROM tags")
    tag_count = cur.fetchone()[0]

    cur.execute("SELECT MIN(pub_year), MAX(pub_year) FROM ref_records WHERE pub_year IS NOT NULL")
    yr = cur.fetchone()
    conn.close()

    return {
        "total_records":  total,
        "tagged_records": tagged,
        "missing_doi":    no_doi,
        "total_tags":     tag_count,
        "year_min":       yr[0],
        "year_max":       yr[1],
    }




@app.get("/related/{key}")
def get_related(key: str, limit: int = Query(10, description="Max related records")):
    """Finds records related to `key` by shared tags, ranked by overlap count."""
    conn = get_db()
    cur = conn.cursor()

    cur.execute("SELECT tag_id FROM ref_tags WHERE key = %s", [key])
    tag_ids = [r["tag_id"] for r in cur.fetchall()]

    if not tag_ids:
        conn.close()
        return {"key": key, "related": []}

    cur.execute("""
        SELECT r.key, r.title, r.author, r.pub_year,
               COUNT(rt.tag_id) as shared_tags
        FROM ref_tags rt
        JOIN ref_records r ON rt.key = r.key
        WHERE rt.tag_id = ANY(%s)
          AND r.key != %s
        GROUP BY r.key, r.title, r.author, r.pub_year
        ORDER BY shared_tags DESC, r.pub_year DESC
        LIMIT %s
    """, [tag_ids, key, limit])
    rows = cur.fetchall()
    conn.close()

    return {
        "key": key,
        "related": [
            {"key": r["key"], "title": r["title"], "author": r["author"],
             "year": r["pub_year"], "shared_tags": r["shared_tags"]}
            for r in rows
        ]
    }


# ── 6. Precompiled knowledge graph (reads from graph_edges table) ────────────

@app.get("/graph")
def get_graph(
    min_weight: int = Query(
        5,
        description="Minimum edge weight"
    ),
    limit: int = Query(
        3000,
        description="Maximum number of returned edges"
    )
):
    """
    Returns a filtered knowledge graph.

    Edges are precomputed by build_graph.py and stored in graph_edges.
    """

    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    # ---------- Nodes ----------
    cur.execute("""
        SELECT
            t.tag_id,
            t.tag_name,
            t.tag_type,
            COUNT(rt.key) AS record_count
        FROM tags t
        LEFT JOIN ref_tags rt
            ON t.tag_id = rt.tag_id
        GROUP BY
            t.tag_id,
            t.tag_name,
            t.tag_type
    """)

    rows = fetchall(cur)

    nodes = [
        {
            "id": f"tag_{r['tag_id']}",
            "label": r["tag_name"],
            "type": r.get("tag_type"),
            "count": r["record_count"]
        }
        for r in rows
    ]

    # ---------- Edges ----------
    cur.execute(f"""
        SELECT
            tag_id_a,
            tag_id_b,
            weight
        FROM graph_edges
        WHERE weight >= {p}
        ORDER BY weight DESC
        LIMIT {p}
    """, [min_weight, limit])

    rows = fetchall(cur)

    edges = [
        {
            "source": f"tag_{r['tag_id_a']}",
            "target": f"tag_{r['tag_id_b']}",
            "weight": r["weight"]
        }
        for r in rows
    ]

    conn.close()

    return {
        "nodes": nodes,
        "edges": edges,
        "edge_count": len(edges),
        "min_weight": min_weight
    }

# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
