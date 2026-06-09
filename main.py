import os
import io
import zipfile
import re
from pathlib import Path

from fastapi import FastAPI, Query, HTTPException, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import psycopg2
import psycopg2.extras

DEFAULT_DSN = None
BASE_DIR = Path(__file__).resolve().parent / "by-year"

app = FastAPI(title="Chandamama Kathalu API", version="1.0.0")


def get_connection_dsn() -> str:
    database_url = os.getenv("DATABASE_URL")

    if not database_url:
        raise RuntimeError(
            "DATABASE_URL environment variable is not set"
        )

    return database_url

    host = os.getenv("PGHOST", "/home/surya/pg_data")
    port = os.getenv("PGPORT", "5433")
    dbname = os.getenv("PGDATABASE", "chandamama")
    user = os.getenv("PGUSER", "surya")
    password = os.getenv("PGPASSWORD")

    if password:
        return f"host={host} port={port} dbname={dbname} user={user} password={password}"
    return f"host={host} port={port} dbname={dbname} user={user}"

API_KEYS = {"demo-key": "Demo User"}

def get_db():
    conn = psycopg2.connect(get_connection_dsn())
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
            CREATE TABLE IF NOT EXISTS stories (
                id SERIAL PRIMARY KEY,
                year INTEGER NOT NULL,
                month TEXT NOT NULL,
                story_number INTEGER NOT NULL,
                folder_name TEXT NOT NULL UNIQUE
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS pages (
                id SERIAL PRIMARY KEY,
                story_id INTEGER NOT NULL REFERENCES stories(id) ON DELETE CASCADE,
                page_number INTEGER NOT NULL,
                file_name TEXT NOT NULL,
                content TEXT NOT NULL,
                content_tsv TSVECTOR
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pages_story_id ON pages(story_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_stories_year_month ON stories(year, month)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_pages_content_tsv ON pages USING GIN (content_tsv)")
        cur.execute("ALTER TABLE pages ADD COLUMN IF NOT EXISTS content_tsv TSVECTOR")
    conn.commit()


def seed_dataset_if_needed(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM stories")
        story_count = cur.fetchone()[0]

    if story_count > 0:
        return

    if not BASE_DIR.exists():
        return

    story_dirs = []
    for item in sorted(BASE_DIR.iterdir()):
        if not item.is_dir():
            continue
        match = re.match(r"^(\d{4})_([a-z]{3})_story_(\d+)$", item.name)
        if match:
            year = int(match.group(1))
            month = match.group(2)
            story_number = int(match.group(3))
            story_dirs.append((item, year, month, story_number))

    with conn.cursor() as cur:
        for folder, year, month, story_number in story_dirs:
            cur.execute(
                "INSERT INTO stories (year, month, story_number, folder_name) VALUES (%s, %s, %s, %s) RETURNING id",
                (year, month, story_number, folder.name),
            )
            story_id = cur.fetchone()[0]

            for page_file in sorted(folder.glob("page_*_corrected.txt")):
                page_match = re.search(r"page_(\d+)", page_file.name)
                page_number = int(page_match.group(1)) if page_match else 0
                content = page_file.read_text(encoding="utf-8", errors="ignore")
                cur.execute(
                    """
                    INSERT INTO pages (story_id, page_number, file_name, content, content_tsv)
                    VALUES (%s, %s, %s, %s, to_tsvector('simple', %s))
                    """,
                    (story_id, page_number, page_file.name, content, content),
                )
    conn.commit()


@app.on_event("startup")
def startup_event() -> None:
    try:
        conn = psycopg2.connect(get_connection_dsn())
        try:
            ensure_schema(conn)
            seed_dataset_if_needed(conn)
        finally:
            conn.close()
    except Exception as exc:
        raise RuntimeError(f"Database initialization failed: {exc}") from exc

def verify_api_key(api_key: str = Query(None, description="API key for authentication")):
    if api_key and api_key in API_KEYS:
        return API_KEYS[api_key]
    raise HTTPException(status_code=403, detail="Invalid or missing API key. Provide ?api_key=demo-key")

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

@app.get("/api/download-database")
def download_database():
    raise HTTPException(
        status_code=501,
        detail="Database export is disabled on Render"
    )
    offset = (page - 1) * per_page
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

    search_query = """
        SELECT p.id, p.content, p.page_number, p.file_name,
               s.id AS story_id, s.year, s.month, s.story_number, s.folder_name,
               ts_rank(p.content_tsv, plainto_tsquery('simple', %s)) AS rank
        FROM pages p
        JOIN stories s ON s.id = p.story_id
        WHERE p.content_tsv @@ plainto_tsquery('simple', %s)
        ORDER BY rank DESC
        OFFSET %s LIMIT %s
    """
    cur.execute(search_query, (q, q, offset, per_page))
    results = cur.fetchall()

    count_query = """
        SELECT COUNT(*) AS total
        FROM pages p
        JOIN stories s ON s.id = p.story_id
        WHERE p.content_tsv @@ plainto_tsquery('simple', %s)
    """
    cur.execute(count_query, (q,))
    total = cur.fetchone()["total"]
    cur.close()

    items = []
    for r in results:
        highlighted = highlight_text(r["content"], q)
        items.append({
            "id": r["id"],
            "story_id": r["story_id"],
            "year": r["year"],
            "month": r["month"],
            "story_number": r["story_number"],
            "folder_name": r["folder_name"],
            "page_number": r["page_number"],
            "file_name": r["file_name"],
            "highlighted_content": highlighted,
        })

    return {
        "query": q,
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
        "results": items,
    }

@app.get("/api/stories", response_class=JSONResponse)
def list_stories(
    year: int = Query(None),
    month: str = Query(None),
    page: int = Query(1, ge=1),
    per_page: int = Query(50, ge=1, le=200),
    db=Depends(get_db),
):
    offset = (page - 1) * per_page
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    conditions = []
    params = []
    if year:
        conditions.append("s.year = %s")
        params.append(year)
    if month:
        conditions.append("s.month = %s")
        params.append(month)

    where = "WHERE " + " AND ".join(conditions) if conditions else ""

    cur.execute(f"SELECT COUNT(*) AS total FROM stories s {where}", params)
    total = cur.fetchone()["total"]

    cur.execute(
        f"SELECT s.*, (SELECT COUNT(*) FROM pages WHERE story_id = s.id) AS page_count FROM stories s {where} ORDER BY s.year, s.month, s.story_number OFFSET %s LIMIT %s",
        params + [offset, per_page],
    )
    stories = cur.fetchall()
    cur.close()

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "total_pages": (total + per_page - 1) // per_page,
        "results": [dict(s) for s in stories],
    }

@app.get("/api/stories/{story_id}", response_class=JSONResponse)
def get_story(story_id: int, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM stories WHERE id = %s", (story_id,))
    story = cur.fetchone()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    cur.execute("SELECT * FROM pages WHERE story_id = %s ORDER BY page_number", (story_id,))
    pages = cur.fetchall()
    cur.close()
    return {**dict(story), "pages": [dict(p) for p in pages]}

@app.get("/api/stories/{story_id}/download")
def download_story(story_id: int, db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT * FROM stories WHERE id = %s", (story_id,))
    story = cur.fetchone()
    if not story:
        raise HTTPException(status_code=404, detail="Story not found")
    cur.execute("SELECT * FROM pages WHERE story_id = %s ORDER BY page_number", (story_id,))
    pages = cur.fetchall()
    cur.close()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in pages:
            zf.writestr(f"{story['folder_name']}/{p['file_name']}", p["content"])
    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename={story['folder_name']}.zip"},
    )

@app.get("/api/download-all")
def download_all(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT s.folder_name, p.file_name, p.content FROM pages p JOIN stories s ON s.id = p.story_id ORDER BY s.year, s.month, s.story_number, p.page_number")
    all_pages = cur.fetchall()
    cur.close()

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for row in all_pages:
            zf.writestr(f"{row['folder_name']}/{row['file_name']}", row["content"])
    zip_buf.seek(0)
    return StreamingResponse(
        zip_buf,
        media_type="application/zip",
        headers={"Content-Disposition": "attachment; filename=chandamama_kathalu_all.zip"},
    )

@app.get("/api/download-database")
def download_database():
    import subprocess
    proc = subprocess.Popen(
        ["pg_dump", "-h", "/home/surya/pg_data", "-p", "5433", "-U", "surya", "-d", "chandamama"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )

    def stream():
        for chunk in iter(lambda: proc.stdout.read(65536), b""):
            yield chunk
        proc.stdout.close()
        proc.wait()

    return StreamingResponse(
        stream(),
        media_type="application/sql",
        headers={"Content-Disposition": "attachment; filename=chandamama_database.sql"},
    )

@app.get("/api/export-sql")
def export_sql(db=Depends(get_db)):
    cur = db.cursor()
    buf = io.StringIO()
    cur.copy_to(buf, "stories", sep="\t", null="\\N")
    cur.copy_to(buf, "pages", sep="\t", null="\\N")
    cur.close()
    buf.seek(0)
    return PlainTextResponse(buf.getvalue())

@app.get("/api/months", response_class=JSONResponse)
def list_months(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT month FROM stories ORDER BY month")
    months = [r["month"] for r in cur.fetchall()]
    cur.close()
    return {"months": months}

@app.get("/api/years", response_class=JSONResponse)
def list_years(db=Depends(get_db)):
    cur = db.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
    cur.execute("SELECT DISTINCT year FROM stories ORDER BY year")
    years = [r["year"] for r in cur.fetchall()]
    cur.close()
    return {"years": years}

def highlight_text(text: str, query: str) -> str:
    words = query.strip().split()
    result = text
    for w in words:
        result = re.sub(
            re.escape(w),
            lambda m: f"<mark>{m.group(0)}</mark>",
            result,
            flags=re.IGNORECASE,
        )
    return result.replace("\n", "<br>")

@app.get("/", response_class=HTMLResponse)
def index():
    with open(os.path.join(STATIC_DIR, "index.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())

@app.get("/story/{story_id}", response_class=HTMLResponse)
def story_page(story_id: int):
    with open(os.path.join(STATIC_DIR, "story.html"), encoding="utf-8") as f:
        html = f.read().replace("{{story_id}}", str(story_id))
        return HTMLResponse(html)

@app.get("/search", response_class=HTMLResponse)
def search_page():
    with open(os.path.join(STATIC_DIR, "search.html"), encoding="utf-8") as f:
        return HTMLResponse(f.read())
