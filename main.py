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


# ── 0. Root / health check ────────────────────────────────────────────────────
@app.get("/")
def root():
    return {
        "status": "ok",
        "service": "AMB Search API",
        "mode": "postgres" if DATABASE_URL else "sqlite",
        "docs": "/docs"
    }


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
    p = PLACEHOLDER

    cur.execute(f"SELECT tag_id FROM ref_tags WHERE key = {p}", [key])
    tag_ids = [r["tag_id"] for r in fetchall(cur)]

    if not tag_ids:
        conn.close()
        return {"key": key, "related": []}

    if DATABASE_URL:
        tag_filter = f"rt.tag_id = ANY({p})"
    else:
        # SQLite has no ANY(); expand to an IN (...) list instead
        tag_filter = f"rt.tag_id IN ({','.join([p] * len(tag_ids))})"
        tag_ids = list(tag_ids)  # keep as separate params below

    cur.execute(f"""
        SELECT r.key, r.title, r.author, r.pub_year,
               COUNT(rt.tag_id) as shared_tags
        FROM ref_tags rt
        JOIN ref_records r ON rt.key = r.key
        WHERE {tag_filter}
          AND r.key != {p}
        GROUP BY r.key, r.title, r.author, r.pub_year
        ORDER BY shared_tags DESC, r.pub_year DESC
        LIMIT {p}
    """, ([tag_ids] if DATABASE_URL else tag_ids) + [key, limit])
    rows = fetchall(cur)
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
    Only nodes that appear in at least one returned edge are included —
    this avoids sending hundreds of disconnected/irrelevant nodes to the
    front end just because they exist in the `tags` table.
    """

    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    # ---------- Edges first (this determines which nodes we actually need) --
    cur.execute(f"""
        SELECT tag_id_a, tag_id_b, weight
        FROM graph_edges
        WHERE weight >= {p}
        ORDER BY weight DESC
        LIMIT {p}
    """, [min_weight, limit])
    edge_rows = fetchall(cur)

    needed_ids = set()
    for r in edge_rows:
        needed_ids.add(r["tag_id_a"])
        needed_ids.add(r["tag_id_b"])

    nodes = []
    if needed_ids:
        id_list = ",".join(str(i) for i in needed_ids)
        cur.execute(f"""
            SELECT t.tag_id, t.tag_name, t.tag_type, COUNT(rt.key) AS record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            WHERE t.tag_id IN ({id_list})
            GROUP BY t.tag_id, t.tag_name, t.tag_type
        """)
        rows = fetchall(cur)
        nodes = [
            {"id": f"tag_{r['tag_id']}", "label": r["tag_name"],
             "type": r.get("tag_type"), "count": r["record_count"]}
            for r in rows
        ]

    edges = [
        {"source": f"tag_{r['tag_id_a']}", "target": f"tag_{r['tag_id_b']}", "weight": r["weight"]}
        for r in edge_rows
    ]

    conn.close()

    return {
        "nodes": nodes,
        "edges": edges,
        "edge_count": len(edges),
        "min_weight": min_weight
    }


# ── 7. Lightweight node search (for a "pick a starting tag" search box) ──────
@app.get("/graph/search")
def search_graph_nodes(
    q: str = Query(..., description="Search text to match against tag names"),
    limit: int = Query(20, description="Max matching tags to return")
):
    """
    Cheap, small-payload search over tag names only — used to populate a
    search/autocomplete box so the user can pick a starting node, instead
    of ever loading the full graph. Returns no edges.
    """
    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    like = f"%{q}%"
    if DATABASE_URL:
        cur.execute(f"""
            SELECT t.tag_id, t.tag_name, t.tag_type, COUNT(rt.key) AS record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            WHERE t.tag_name ILIKE {p}
            GROUP BY t.tag_id, t.tag_name, t.tag_type
            ORDER BY record_count DESC
            LIMIT {p}
        """, [like, limit])
    else:
        cur.execute(f"""
            SELECT t.tag_id, t.tag_name, t.tag_type, COUNT(rt.key) AS record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            WHERE t.tag_name LIKE {p}
            GROUP BY t.tag_id, t.tag_name, t.tag_type
            ORDER BY record_count DESC
            LIMIT {p}
        """, [like, limit])

    rows = fetchall(cur)
    conn.close()

    return {
        "results": [
            {"id": f"tag_{r['tag_id']}", "label": r["tag_name"],
             "type": r.get("tag_type"), "count": r["record_count"]}
            for r in rows
        ]
    }


# ── 8. Ego-graph: one node + its direct neighbors only ───────────────────────
@app.get("/graph/node/{tag_id}")
def get_node_neighborhood(
    tag_id: int,
    min_weight: int = Query(1, description="Minimum edge weight to include"),
    limit_neighbors: int = Query(15, description="Max neighboring nodes to return")
):
    """
    Returns ONE node plus only its directly-connected neighbors (its
    strongest co-occurring tags), not the whole graph. This is what the
    front end should call:
      1. right after the user picks a starting tag via /graph/search
      2. again whenever the user clicks a node to "expand" it further

    Each call stays small regardless of how big the overall graph is,
    which is what avoids the white-screen crash from rendering thousands
    of nodes/edges at once.
    """
    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    cur.execute(f"""
        SELECT tag_id, tag_name, tag_type FROM tags WHERE tag_id = {p}
    """, [tag_id])
    center = fetchone(cur)
    if not center:
        conn.close()
        return {"error": "tag not found"}

    cur.execute(f"""
        SELECT tag_id_a, tag_id_b, weight
        FROM graph_edges
        WHERE (tag_id_a = {p} OR tag_id_b = {p})
          AND weight >= {p}
        ORDER BY weight DESC
        LIMIT {p}
    """, [tag_id, tag_id, min_weight, limit_neighbors])
    edge_rows = fetchall(cur)

    neighbor_ids = set()
    for r in edge_rows:
        other = r["tag_id_b"] if r["tag_id_a"] == tag_id else r["tag_id_a"]
        neighbor_ids.add(other)

    neighbor_nodes = []
    if neighbor_ids:
        id_list = ",".join(str(i) for i in neighbor_ids)
        cur.execute(f"""
            SELECT t.tag_id, t.tag_name, t.tag_type, COUNT(rt.key) AS record_count
            FROM tags t
            LEFT JOIN ref_tags rt ON t.tag_id = rt.tag_id
            WHERE t.tag_id IN ({id_list})
            GROUP BY t.tag_id, t.tag_name, t.tag_type
        """)
        rows = fetchall(cur)
        neighbor_nodes = [
            {"id": f"tag_{r['tag_id']}", "label": r["tag_name"],
             "type": r.get("tag_type"), "count": r["record_count"]}
            for r in rows
        ]

    # Center node's own record count
    cur.execute(f"SELECT COUNT(*) FROM ref_tags WHERE tag_id = {p}", [tag_id])
    center_count = cur.fetchone()[0]

    conn.close()

    nodes = [{
        "id": f"tag_{center['tag_id']}", "label": center["tag_name"],
        "type": center.get("tag_type"), "count": center_count
    }] + neighbor_nodes

    edges = [
        {"source": f"tag_{r['tag_id_a']}", "target": f"tag_{r['tag_id_b']}", "weight": r["weight"]}
        for r in edge_rows
    ]

    return {"nodes": nodes, "edges": edges, "center_id": f"tag_{tag_id}"}


# ── 9. Literature-level graph: search for a starting paper ───────────────────
@app.get("/graph/record/search")
def search_graph_records(
    q: str = Query(..., description="Search text to match against titles"),
    limit: int = Query(10, description="Max matching records to return")
):
    """
    Cheap search over record titles only — used to populate a search box
    so the user can pick a starting PAPER (not a tag) for the literature
    relationship graph. Mirrors /graph/search but for records.

    Splits the query into individual words and requires the title to
    contain ALL of them (in any order/position), not the exact phrase —
    so "Tibet medicine" also matches a title like "Traditional Medicine
    in Tibet", not just titles containing that literal substring.
    """
    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    words = [w for w in q.strip().split() if w]
    if not words:
        conn.close()
        return {"results": []}

    op = "ILIKE" if DATABASE_URL else "LIKE"
    conditions = " AND ".join([f"title {op} {p}"] * len(words))
    params = [f"%{w}%" for w in words]

    cur.execute(f"""
        SELECT key, title, author, pub_year
        FROM ref_records
        WHERE {conditions}
        ORDER BY pub_year DESC NULLS LAST
        LIMIT {p}
    """, params + [limit])
    rows = fetchall(cur)
    conn.close()

    return {
        "results": [
            {"key": r["key"], "title": r["title"], "author": r["author"], "year": r["pub_year"]}
            for r in rows
        ]
    }


# ── 10. Literature-level ego-graph: one paper + its most similar papers ──────
@app.get("/graph/record/{key}")
def get_record_neighborhood(
    key: str,
    min_weight: int = Query(2, description="Minimum shared-tag count to include"),
    limit_neighbors: int = Query(12, description="Max neighboring records to return")
):
    """
    Returns ONE record plus only its most similar neighbors (by shared-tag
    weight, precomputed by build_graph.py into record_edges) — the
    literature equivalent of /graph/node/{tag_id}. Same click-to-expand
    pattern: call this again with a neighbor's key to keep growing the
    graph, instead of ever loading all record relationships at once.
    """
    conn = get_db()
    cur = conn.cursor()
    p = PLACEHOLDER

    cur.execute(f"SELECT key, title, author, pub_year FROM ref_records WHERE key = {p}", [key])
    center = fetchone(cur)
    if not center:
        conn.close()
        return {"error": "record not found"}

    cur.execute(f"""
        SELECT key_a, key_b, weight
        FROM record_edges
        WHERE (key_a = {p} OR key_b = {p})
          AND weight >= {p}
        ORDER BY weight DESC
        LIMIT {p}
    """, [key, key, min_weight, limit_neighbors])
    edge_rows = fetchall(cur)

    neighbor_keys = set()
    for r in edge_rows:
        other = r["key_b"] if r["key_a"] == key else r["key_a"]
        neighbor_keys.add(other)

    neighbor_nodes = []
    if neighbor_keys:
        placeholders = ",".join([p] * len(neighbor_keys))
        cur.execute(f"""
            SELECT key, title, author, pub_year FROM ref_records
            WHERE key IN ({placeholders})
        """, list(neighbor_keys))
        rows = fetchall(cur)
        neighbor_nodes = [
            {"id": r["key"], "label": r["title"], "author": r["author"], "year": r["pub_year"]}
            for r in rows
        ]

    conn.close()

    nodes = [{
        "id": center["key"], "label": center["title"],
        "author": center["author"], "year": center["pub_year"]
    }] + neighbor_nodes

    edges = [
        {"source": r["key_a"], "target": r["key_b"], "weight": r["weight"]}
        for r in edge_rows
    ]

    return {"nodes": nodes, "edges": edges, "center_id": key}


# ── Run ───────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
