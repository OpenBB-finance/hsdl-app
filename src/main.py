import base64
import asyncio
import json
import logging
import sqlite3
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import List, Optional, Union

from fastapi import Body, FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware

from .config import settings

log = logging.getLogger(__name__)

USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_REQUEST_HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept": "application/pdf,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "identity",
}
_PDF_CACHE: dict[str, bytes] = {}
_PDF_CACHE_MAX = 256


def _download_pdf(url: str) -> bytes:
    cached = _PDF_CACHE.get(url)
    if cached is not None:
        return cached
    req = urllib.request.Request(url, headers=_REQUEST_HEADERS)
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = resp.read()
    if len(_PDF_CACHE) >= _PDF_CACHE_MAX:
        oldest = next(iter(_PDF_CACHE))
        del _PDF_CACHE[oldest]
    _PDF_CACHE[url] = data
    return data


DB_PATH = Path(settings.hsdl_db_path)
HIERARCHY_PATH = DB_PATH.with_name("hsdl_catalog_hierarchy.json")
WIDGETS_PATH = Path(__file__).parent.parent / "widgets.json"
APPS_PATH = Path(__file__).parent.parent / "apps.json"
STATIC_DIR = Path(__file__).parent
PUBLIC_STATIC_FILES: dict[str, tuple[str, str]] = {
    "openbb-logo.svg": ("openbb-logo.svg", "image/svg+xml"),
    "openbb-hsdl-screenshot1.png": ("openbb-hsdl-screenshot1.png", "image/png"),
    "openbb-hsdl-screenshot2.png": ("openbb-hsdl-screenshot2.png", "image/png"),
}

_SYNC_INTERVAL = 12 * 3600
_sync_task: asyncio.Task | None = None


async def _sync_loop():
    from .sync import incremental_sync

    while True:
        await asyncio.sleep(_SYNC_INTERVAL)
        try:
            log.info("Starting incremental sync")
            result = incremental_sync(DB_PATH)
            log.info("Incremental sync complete: %s new documents", result["added"])
        except Exception:
            log.exception("Incremental sync failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _sync_task
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )
    _sync_task = asyncio.create_task(_sync_loop())
    yield
    _sync_task.cancel()
    conn = get_connection()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.close()


app = FastAPI(title="HSDL OpenBB API", version="0.1.0", lifespan=lifespan)


class RequireOpenBBUserMiddleware(BaseHTTPMiddleware):
    _EXEMPT_PATHS = {"/health", "/"}
    _EXEMPT_PREFIXES = ("/static/",)

    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        if (
            request.method == "OPTIONS"
            or path in self._EXEMPT_PATHS
            or path.startswith(self._EXEMPT_PREFIXES)
        ):
            return await call_next(request)
        if not request.headers.get("x-openbb-user"):
            return JSONResponse(
                status_code=403, content={"detail": "Missing required header"}
            )
        return await call_next(request)


_CACHE_RULES: list[tuple[str, str]] = [
    ("/hsdl/view-url", "public, max-age=604800, immutable"),
    ("/hsdl/hierarchy", "public, max-age=3600, stale-while-revalidate=600"),
    ("/hsdl/documents/", "public, max-age=300, stale-while-revalidate=60"),
    ("/hsdl/", "public, max-age=60, stale-while-revalidate=30"),
]


class CacheControlMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        if request.method == "GET" or request.method == "POST":
            path = request.url.path
            for prefix, directive in _CACHE_RULES:
                if path.startswith(prefix):
                    response.headers["Cache-Control"] = directive
                    break
        return response


app.add_middleware(CacheControlMiddleware)
app.add_middleware(RequireOpenBBUserMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pro.openbb.co",
        "https://pro.openbb.dev",
        "http://localhost:1420",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class FileOption(BaseModel):
    label: str
    value: str


class FileDataFormat(BaseModel):
    data_type: str
    filename: str


class DataUrl(BaseModel):
    url: str
    data_format: FileDataFormat


class DataContent(BaseModel):
    content: str
    data_format: FileDataFormat


class DataError(BaseModel):
    error_type: str
    content: str


def get_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA mmap_size=268435456")
    conn.execute("PRAGMA cache_size=-64000")
    return conn


def normalize_filter(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or value.lower() == "all":
        return None
    return value


def build_document_filter_sql(
    resource_group: Optional[str] = None,
    subject: Optional[str] = None,
    publisher: Optional[str] = None,
    query: Optional[str] = None,
    year: Optional[str] = None,
):
    joins = []
    where = []
    params: List[str] = []

    if resource_group:
        joins.append("JOIN document_resource_groups rg ON d.docid = rg.docid")
        where.append("rg.value = ?")
        params.append(resource_group)

    if subject:
        joins.append("JOIN document_subjects ds ON d.docid = ds.docid")
        where.append("ds.value = ?")
        params.append(subject)

    if publisher:
        joins.append("JOIN document_publishers dp ON d.docid = dp.docid")
        where.append("dp.value = ?")
        params.append(publisher)

    if query:
        if query.isdigit():
            where.append("d.docid = ?")
            params.append(int(query))
        else:
            joins.append("JOIN document_search fts ON d.docid = fts.docid")
            where.append("document_search MATCH ?")
            params.append(query)

    if year:
        where.append("SUBSTR(d.publish_date, 1, 4) = ?")
        params.append(year)

    join_sql = " ".join(dict.fromkeys(joins))
    where_sql = f"WHERE {' AND '.join(where)}" if where else ""
    return join_sql, where_sql, params


def option_label(row) -> str:
    date = row["publish_date"] or ""
    group = row["resource_group"] or ""
    parts = [row["title"]]
    suffix = " | ".join(part for part in [date, group] if part)
    if suffix:
        parts.append(suffix)
    return " | ".join(parts)


@app.get("/")
def root():
    return {"message": "HSDL OpenBB Backend"}


@app.get("/widgets.json")
def get_widgets():
    return JSONResponse(content=json.loads(WIDGETS_PATH.read_text(encoding="utf-8")))


@app.get("/apps.json")
def get_apps():
    return JSONResponse(content=json.loads(APPS_PATH.read_text(encoding="utf-8")))


@app.get("/static/{filename}")
def serve_static(filename: str):
    entry = PUBLIC_STATIC_FILES.get(filename)
    if entry is None:
        return JSONResponse({"detail": "Not found"}, status_code=404)
    name, media_type = entry
    return FileResponse(STATIC_DIR / name, media_type=media_type)


@app.get("/health")
def health():
    if not DB_PATH.exists():
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": "Database not found"},
        )
    try:
        conn = get_connection()
        row = conn.execute("SELECT COUNT(*) FROM documents").fetchone()
        conn.close()
        doc_count = row[0]
    except Exception as exc:
        return JSONResponse(
            status_code=503,
            content={"status": "unavailable", "detail": str(exc)},
        )
    return {"status": "ok", "database": str(DB_PATH), "documents": doc_count}


@app.get("/hsdl/years/options", response_model=List[FileOption])
def get_year_options(
    resource_group: str = Query("all"),
    subject: str = Query("all"),
    publisher: str = Query("all"),
    query: Optional[str] = Query(None),
):
    resource_group = normalize_filter(resource_group)
    subject = normalize_filter(subject)
    publisher = normalize_filter(publisher)
    query = normalize_filter(query)

    filters = []
    params: list = []

    if resource_group:
        filters.append(
            "docid IN (SELECT docid FROM document_resource_groups WHERE value = ?)"
        )
        params.append(resource_group)
    if subject:
        filters.append("docid IN (SELECT docid FROM document_subjects WHERE value = ?)")
        params.append(subject)
    if publisher:
        filters.append(
            "docid IN (SELECT docid FROM document_publishers WHERE value = ?)"
        )
        params.append(publisher)
    if query:
        if query.isdigit():
            filters.append("docid = ?")
            params.append(int(query))
        else:
            filters.append(
                "docid IN (SELECT docid FROM document_search WHERE document_search MATCH ?)"
            )
            params.append(query)

    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"SELECT SUBSTR(publish_date, 1, 4) AS year, COUNT(*) AS total FROM documents{where} GROUP BY SUBSTR(publish_date, 1, 4) HAVING year GLOB '[0-9][0-9][0-9][0-9]' ORDER BY year DESC"
    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    options = [FileOption(label="All", value="all")]
    options.extend(
        FileOption(label=f"{row['year']} ({row['total']})", value=row["year"])
        for row in rows
    )
    return options


@app.get("/hsdl/resource-groups/options", response_model=List[FileOption])
def get_resource_group_options():
    conn = get_connection()
    rows = conn.execute(
        "SELECT value, COUNT(*) AS total FROM document_resource_groups GROUP BY value ORDER BY total DESC, value ASC"
    ).fetchall()
    conn.close()

    options = [FileOption(label="All", value="all")]
    options.extend(
        FileOption(label=f"{row['value']} ({row['total']})", value=row["value"])
        for row in rows
    )
    return options


@app.get("/hsdl/subjects/options", response_model=List[FileOption])
def get_subject_options(
    resource_group: str = Query("all"),
    year: str = Query("all"),
    query: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    resource_group = normalize_filter(resource_group)
    year = normalize_filter(year)
    query = normalize_filter(query)

    params: list = []

    if not resource_group and not query and not year:
        sql = "SELECT value, COUNT(*) AS total FROM document_subjects GROUP BY value ORDER BY total DESC, value ASC LIMIT ?"
        params.append(limit)
    else:
        filters = []
        if resource_group:
            filters.append(
                "docid IN (SELECT docid FROM document_resource_groups WHERE value = ?)"
            )
            params.append(resource_group)
        if year:
            filters.append(
                "docid IN (SELECT docid FROM documents WHERE SUBSTR(publish_date, 1, 4) = ?)"
            )
            params.append(year)
        if query:
            if query.isdigit():
                filters.append("docid = ?")
                params.append(int(query))
            else:
                filters.append(
                    "docid IN (SELECT docid FROM document_search WHERE document_search MATCH ?)"
                )
                params.append(query)
        where = " AND ".join(filters)
        sql = f"SELECT value, COUNT(*) AS total FROM document_subjects WHERE {where} GROUP BY value ORDER BY total DESC, value ASC LIMIT ?"
        params.append(limit)

    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    options = [FileOption(label="All", value="all")]
    options.extend(
        FileOption(label=f"{row['value']} ({row['total']})", value=row["value"])
        for row in rows
    )
    return options


@app.get("/hsdl/publishers/options", response_model=List[FileOption])
def get_publisher_options(
    resource_group: str = Query("all"),
    subject: str = Query("all"),
    year: str = Query("all"),
    query: Optional[str] = Query(None),
    limit: int = Query(200, ge=1, le=1000),
):
    resource_group = normalize_filter(resource_group)
    subject = normalize_filter(subject)
    year = normalize_filter(year)
    query = normalize_filter(query)

    params: list = []
    filters = []

    if resource_group:
        filters.append(
            "docid IN (SELECT docid FROM document_resource_groups WHERE value = ?)"
        )
        params.append(resource_group)
    if subject:
        filters.append("docid IN (SELECT docid FROM document_subjects WHERE value = ?)")
        params.append(subject)
    if year:
        filters.append(
            "docid IN (SELECT docid FROM documents WHERE SUBSTR(publish_date, 1, 4) = ?)"
        )
        params.append(year)
    if query:
        if query.isdigit():
            filters.append("docid = ?")
            params.append(int(query))
        else:
            filters.append(
                "docid IN (SELECT docid FROM document_search WHERE document_search MATCH ?)"
            )
            params.append(query)

    where = (" WHERE " + " AND ".join(filters)) if filters else ""
    sql = f"SELECT value, COUNT(*) AS total FROM document_publishers{where} GROUP BY value ORDER BY total DESC, value ASC LIMIT ?"
    params.append(limit)

    conn = get_connection()
    rows = conn.execute(sql, params).fetchall()
    conn.close()

    options = [FileOption(label="All", value="all")]
    options.extend(
        FileOption(label=f"{row['value']} ({row['total']})", value=row["value"])
        for row in rows
    )
    return options


@app.get("/hsdl/documents/options", response_model=List[FileOption])
def get_document_options(
    resource_group: str = Query("all"),
    subject: str = Query("all"),
    publisher: str = Query("all"),
    year: str = Query("all"),
    query: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    resource_group = normalize_filter(resource_group)
    subject = normalize_filter(subject)
    publisher = normalize_filter(publisher)
    year = normalize_filter(year)
    query = normalize_filter(query)

    join_sql, where_sql, params = build_document_filter_sql(
        resource_group=resource_group,
        subject=subject,
        publisher=publisher,
        query=query,
        year=year,
    )

    conn = get_connection()

    if join_sql or where_sql:
        docid_sql = f"SELECT DISTINCT d.docid FROM documents d {join_sql} {where_sql} ORDER BY d.publish_date DESC, d.docid DESC LIMIT ?"
        params.append(limit)
        docid_rows = conn.execute(docid_sql, params).fetchall()
        docids = [r["docid"] for r in docid_rows]
    else:
        docids = [
            r["docid"]
            for r in conn.execute(
                "SELECT docid FROM documents ORDER BY publish_date DESC, docid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    if not docids:
        conn.close()
        return []

    ph = ",".join("?" for _ in docids)
    rows = conn.execute(
        f"SELECT docid, title, publish_date FROM documents WHERE docid IN ({ph}) ORDER BY publish_date DESC, docid DESC",
        docids,
    ).fetchall()

    rg_rows = conn.execute(
        f"SELECT docid, value FROM document_resource_groups WHERE docid IN ({ph})",
        docids,
    ).fetchall()
    conn.close()
    rg_map: dict[int, str] = {}
    for r in rg_rows:
        if r["docid"] not in rg_map:
            rg_map[r["docid"]] = r["value"]
        else:
            rg_map[r["docid"]] += ", " + r["value"]

    result = []
    for row in rows:
        combined = dict(row)
        combined["resource_group"] = rg_map.get(row["docid"], "")
        result.append(FileOption(label=option_label(combined), value=str(row["docid"])))
    return result


@app.get("/hsdl/hierarchy")
def get_hierarchy():
    return JSONResponse(content=json.loads(HIERARCHY_PATH.read_text(encoding="utf-8")))


@app.post("/hsdl/view-url")
async def view_documents_url(
    document: List[str] = Body(default=[], embed=True),
) -> List[Union[DataContent, DataError]]:
    docids = []
    for value in document:
        try:
            docids.append(int(value))
        except ValueError:
            pass

    if not docids:
        return JSONResponse(headers={"Content-Type": "application/json"}, content=[])

    placeholders = ",".join("?" for _ in docids)
    conn = get_connection()
    rows = conn.execute(
        f"SELECT docid, title, file_type, source_url, view_url FROM documents WHERE docid IN ({placeholders})",
        docids,
    ).fetchall()
    conn.close()

    row_map = {str(row["docid"]): row for row in rows}
    files = []
    for value in document:
        row = row_map.get(value)
        if not row:
            files.append(
                DataError(
                    error_type="not_found", content=f"Document '{value}' not found"
                ).model_dump()
            )
            continue

        view = row["view_url"]
        source = row["source_url"]
        if not view and not source:
            files.append(
                DataError(
                    error_type="not_found", content=f"URL not found for '{value}'"
                ).model_dump()
            )
            continue

        pdf_bytes = None
        for url in [u for u in (view, source) if u]:
            try:
                pdf_bytes = _download_pdf(url)
                break
            except Exception:
                log.debug("URL failed: %s", url)

        if pdf_bytes is None:
            log.warning("All URLs failed for docid %s", value)
            files.append(
                DataError(
                    error_type="download_error",
                    content=f"Failed to download document '{value}'",
                ).model_dump()
            )
            continue

        b64 = base64.b64encode(pdf_bytes).decode("ascii")

        extension = row["file_type"] or "pdf"
        filename = f"{row['title']}.{extension}"
        files.append(
            DataContent(
                content=b64,
                data_format=FileDataFormat(data_type="pdf", filename=filename),
            ).model_dump()
        )

    return JSONResponse(
        headers={"Content-Type": "application/json"},
        content=files,
    )


@app.get("/hsdl/documents/search")
def search_documents(
    resource_group: str = Query("all"),
    subject: str = Query("all"),
    publisher: str = Query("all"),
    year: str = Query("all"),
    query: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
):
    resource_group = normalize_filter(resource_group)
    subject = normalize_filter(subject)
    publisher = normalize_filter(publisher)
    year = normalize_filter(year)
    query = normalize_filter(query)

    join_sql, where_sql, params = build_document_filter_sql(
        resource_group=resource_group,
        subject=subject,
        publisher=publisher,
        query=query,
        year=year,
    )

    conn = get_connection()

    if join_sql or where_sql:
        docid_sql = f"SELECT DISTINCT d.docid FROM documents d {join_sql} {where_sql} ORDER BY d.publish_date DESC, d.docid DESC LIMIT ?"
        params.append(limit)
        docid_rows = conn.execute(docid_sql, params).fetchall()
        docids = [r["docid"] for r in docid_rows]
    else:
        docids = [
            r["docid"]
            for r in conn.execute(
                "SELECT docid FROM documents ORDER BY publish_date DESC, docid DESC LIMIT ?",
                (limit,),
            ).fetchall()
        ]

    if not docids:
        conn.close()
        return []

    ph = ",".join("?" for _ in docids)
    rows = conn.execute(
        f"SELECT docid, title, publish_date, summary, source_url FROM documents WHERE docid IN ({ph})",
        docids,
    ).fetchall()

    rg_rows = conn.execute(
        f"SELECT docid, value FROM document_resource_groups WHERE docid IN ({ph})",
        docids,
    ).fetchall()
    rg_map: dict[int, str] = {}
    for r in rg_rows:
        rg_map.setdefault(r["docid"], [])
        rg_map[r["docid"]].append(r["value"])

    pub_rows = conn.execute(
        f"SELECT docid, value FROM document_publishers WHERE docid IN ({ph})", docids
    ).fetchall()
    conn.close()
    pub_map: dict[int, str] = {}
    for r in pub_rows:
        pub_map.setdefault(r["docid"], [])
        pub_map[r["docid"]].append(r["value"])

    doc_map = {}
    for row in rows:
        d = dict(row)
        did = d["docid"]
        d["docid"] = str(did)
        d["resource_group"] = ", ".join(rg_map.get(did, []))
        d["publisher"] = ", ".join(pub_map.get(did, []))
        doc_map[did] = d

    return [doc_map[did] for did in docids if did in doc_map]


@app.get("/hsdl/documents/{docid}")
def get_document(docid: int):
    conn = get_connection()
    rows = conn.execute("SELECT * FROM documents WHERE docid = ?", (docid,)).fetchall()
    if not rows:
        conn.close()
        return JSONResponse(status_code=404, content={"detail": "Document not found"})
    row = rows[0]

    def collect(table_name: str):
        values = conn.execute(
            f"SELECT value FROM {table_name} WHERE docid = ? ORDER BY value ASC",
            (docid,),
        ).fetchall()
        return [value[0] for value in values]

    payload = dict(row)
    payload["publisher"] = collect("document_publishers")
    payload["creator"] = collect("document_creators")
    payload["series"] = collect("document_series")
    payload["format"] = collect("document_formats")
    payload["resource_group"] = collect("document_resource_groups")
    payload["subjects"] = collect("document_subjects")
    payload["coverage_country"] = collect("document_countries")
    conn.close()

    return JSONResponse(content=payload)
