import argparse
import json
import re
import sqlite3
import time
import urllib.request
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlencode

from .config import settings

SEARCH_URL = "https://www.hsdl.org/c/search?collection=documents"
AJAX_URL = "https://www.hsdl.org/c/wp-admin/admin-ajax.php"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
EXPORT_FIELDS = [
    "DocID",
    "Collection",
    "Title_text",
    "Summary",
    "Description_text",
    "DisplayDate",
    "PublishDate",
    "FileDate",
    "DateOfRecordEntry",
    "Publisher_text",
    "Creator_nostem",
    "Series_text",
    "Format",
    "CoverageCountry",
    "TabSection",
    "Subjects",
    "ExternalDocSource",
    "URL_text",
    "FileType",
    "isexternal",
]

_PUBLISHER_ALIASES = {
    "United States. Government Printing Office": "United States. Government Publishing Office",
    "United States. Government Printing Office. Federal Security Agency": "United States. Government Publishing Office",
    "United States. Government Printing Office. Federal Works Agency": "United States. Government Publishing Office",
    "United States. Government Printing Office. Office for Emergency Management": "United States. Government Publishing Office",
    "United States. General Accounting Office": "United States. Government Accountability Office",
}

_EXCLUDED_RESOURCE_GROUPS = {
    "Webcasts (audio/video)",
}

_SUMMARY_PREFIXES = [
    'From the document: "',
    "From the document: ",
    'The following excerpt from the document contains multiple concealed hyperlinks embedded in the original text. From the document: "',
    "The following excerpt from the document contains multiple concealed hyperlinks embedded in the original text. From the document: ",
    'The passage that follows includes several links embedded in the original text. From the document: "',
    "The passage that follows includes several links embedded in the original text. From the document: ",
]


def clean_summary(text):
    if not text:
        return text
    for prefix in _SUMMARY_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            if text.endswith('"'):
                text = text[:-1]
            break
    return text.strip()


def clean_display_date(text):
    if not text:
        return text
    return text.rstrip("?").strip()


def clean_publish_date(text):
    if not text:
        return text
    if text.endswith("T00:00:00Z"):
        return text[:-10]
    m = re.match(r"^(\d{4}-\d{2}-\d{2})T", text)
    if m:
        return m.group(1)
    return text


def ensure_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return [item for item in value if item]
    if value:
        return [value]
    return []


def clean_publishers(values):
    return [_PUBLISHER_ALIASES.get(v, v) for v in ensure_list(values)]


def join_values(values):
    return " | ".join(ensure_list(values))


def fetch_text(url, data=None, timeout=120):
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "identity",
    }
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded; charset=UTF-8"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read().decode("utf-8", "ignore")


def get_public_documents_query():
    html = fetch_text(SEARCH_URL)
    match = re.search(r"xquery:'([^']+)'", html)
    if not match:
        raise ValueError("Could not find xquery on HSDL public documents search page")
    return match.group(1)


def patch_query(base_query, rows, start):
    parts = dict(parse_qsl(base_query, keep_blank_values=True))
    parts["rows"] = str(rows)
    parts["start"] = str(start)
    parts["fl"] = ",".join(EXPORT_FIELDS)
    return urlencode(parts)


def query_page(query, start, rows):
    patched_query = patch_query(query, rows=rows, start=start)
    data = urlencode(
        {
            "action": "hsdlquery",
            "query": patched_query,
            "start": str(start),
            "rows": str(rows),
            "fq": "[]",
            "rfq": "",
        }
    ).encode()
    payload = fetch_text(AJAX_URL, data=data)
    return json.loads(payload)


def normalize_doc(doc):
    url_text = doc.get("URL_text")
    if url_text and url_text.startswith("/"):
        url_text = f"https://www.hsdl.org{url_text}"
    docid = doc.get("DocID")
    view_url = f"https://www.hsdl.org/c/view?docid={docid}" if docid else None
    return {
        "docid": docid,
        "collection": doc.get("Collection"),
        "title": doc.get("Title_text"),
        "summary": doc.get("Summary"),
        "description": doc.get("Description_text"),
        "display_date": doc.get("DisplayDate"),
        "publish_date": doc.get("PublishDate"),
        "file_date": doc.get("FileDate"),
        "record_entry_date": doc.get("DateOfRecordEntry"),
        "publisher": clean_publishers(doc.get("Publisher_text")),
        "creator": doc.get("Creator_nostem"),
        "series": doc.get("Series_text"),
        "format": doc.get("Format"),
        "resource_group": [
            v
            for v in ensure_list(doc.get("TabSection"))
            if v not in _EXCLUDED_RESOURCE_GROUPS
        ],
        "subjects": doc.get("Subjects"),
        "coverage_country": doc.get("CoverageCountry"),
        "external_doc_source": doc.get("ExternalDocSource"),
        "file_type": doc.get("FileType"),
        "is_external": doc.get("isexternal"),
        "source_url": url_text,
        "abstract_url": (
            f"https://www.hsdl.org/c/abstract/?docid={docid}" if docid else None
        ),
        "view_url": view_url,
    }


def create_schema(conn):
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        PRAGMA synchronous=NORMAL;

        DROP TABLE IF EXISTS documents;
        DROP TABLE IF EXISTS document_publishers;
        DROP TABLE IF EXISTS document_creators;
        DROP TABLE IF EXISTS document_series;
        DROP TABLE IF EXISTS document_formats;
        DROP TABLE IF EXISTS document_resource_groups;
        DROP TABLE IF EXISTS document_subjects;
        DROP TABLE IF EXISTS document_countries;
        DROP TABLE IF EXISTS document_search;

        CREATE TABLE documents (
            docid INTEGER PRIMARY KEY,
            collection INTEGER,
            title TEXT,
            summary TEXT,
            description TEXT,
            display_date TEXT,
            publish_date TEXT,
            file_date TEXT,
            record_entry_date TEXT,
            external_doc_source TEXT,
            file_type TEXT,
            is_external INTEGER,
            source_url TEXT,
            abstract_url TEXT,
            view_url TEXT
        );

        CREATE TABLE document_publishers (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_creators (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_series (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_formats (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_resource_groups (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_subjects (docid INTEGER NOT NULL, value TEXT NOT NULL);
        CREATE TABLE document_countries (docid INTEGER NOT NULL, value TEXT NOT NULL);

        CREATE VIRTUAL TABLE document_search USING fts5(
            docid UNINDEXED, title, summary, description,
            publishers, creators, series, formats,
            resource_groups, subjects, countries,
            tokenize = 'porter unicode61'
        );

        CREATE INDEX idx_document_publishers_value ON document_publishers(value);
        CREATE INDEX idx_document_publishers_docid ON document_publishers(docid);
        CREATE INDEX idx_document_creators_value ON document_creators(value);
        CREATE INDEX idx_document_creators_docid ON document_creators(docid);
        CREATE INDEX idx_document_series_value ON document_series(value);
        CREATE INDEX idx_document_series_docid ON document_series(docid);
        CREATE INDEX idx_document_formats_value ON document_formats(value);
        CREATE INDEX idx_document_formats_docid ON document_formats(docid);
        CREATE INDEX idx_document_resource_groups_value ON document_resource_groups(value);
        CREATE INDEX idx_document_resource_groups_docid ON document_resource_groups(docid);
        CREATE INDEX idx_document_subjects_value ON document_subjects(value);
        CREATE INDEX idx_document_subjects_docid ON document_subjects(docid);
        CREATE INDEX idx_document_countries_value ON document_countries(value);
        CREATE INDEX idx_document_countries_docid ON document_countries(docid);
        CREATE INDEX idx_documents_publish_date ON documents(publish_date DESC);
        CREATE INDEX idx_documents_display_date ON documents(display_date DESC);
    """)


def insert_doc(conn, rec):
    docid = rec["docid"]

    conn.execute(
        """
        INSERT INTO documents (
            docid, collection, title, summary, description, display_date,
            publish_date, file_date, record_entry_date, external_doc_source,
            file_type, is_external, source_url, abstract_url, view_url
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            docid,
            rec.get("collection"),
            rec.get("title"),
            clean_summary(rec.get("summary")),
            clean_summary(rec.get("description")),
            clean_display_date(rec.get("display_date")),
            clean_publish_date(rec.get("publish_date")),
            clean_publish_date(rec.get("file_date")),
            clean_publish_date(rec.get("record_entry_date")),
            rec.get("external_doc_source"),
            rec.get("file_type"),
            int(bool(rec.get("is_external"))),
            rec.get("source_url"),
            rec.get("abstract_url"),
            rec.get("view_url"),
        ),
    )

    for table, key in [
        ("document_publishers", "publisher"),
        ("document_creators", "creator"),
        ("document_series", "series"),
        ("document_formats", "format"),
        ("document_resource_groups", "resource_group"),
        ("document_subjects", "subjects"),
        ("document_countries", "coverage_country"),
    ]:
        rows = [(docid, v) for v in ensure_list(rec.get(key))]
        if rows:
            conn.executemany(f"INSERT INTO {table} (docid, value) VALUES (?, ?)", rows)

    conn.execute(
        """
        INSERT INTO document_search (
            docid, title, summary, description, publishers, creators,
            series, formats, resource_groups, subjects, countries
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """,
        (
            docid,
            rec.get("title", ""),
            clean_summary(rec.get("summary")) or "",
            clean_summary(rec.get("description")) or "",
            join_values(rec.get("publisher")),
            join_values(rec.get("creator")),
            join_values(rec.get("series")),
            join_values(rec.get("format")),
            join_values(rec.get("resource_group")),
            join_values(rec.get("subjects")),
            join_values(rec.get("coverage_country")),
        ),
    )


def sync(database_path, rows_per_page=10000, delay=0.0, max_records=None):
    query = get_public_documents_query()
    first_page = query_page(query, start=0, rows=rows_per_page)
    total = first_page.get("total", 0)
    total_to_fetch = min(total, max_records) if max_records else total

    conn = sqlite3.connect(database_path)
    create_schema(conn)

    resource_group_counts = Counter()
    publisher_counts = Counter()
    format_counts = Counter()
    country_counts = Counter()
    subject_counts = Counter()
    group_publishers = defaultdict(Counter)
    group_subjects = defaultdict(Counter)
    group_formats = defaultdict(Counter)

    fetched = 0
    page_index = 0
    current_page = first_page

    try:
        while fetched < total_to_fetch:
            docs = current_page.get("docs", [])
            if not docs:
                break

            for doc in docs:
                if fetched >= total_to_fetch:
                    break
                formats = doc.get("Format")
                if isinstance(formats, list):
                    if "application/pdf" not in formats:
                        continue
                elif formats != "application/pdf":
                    continue
                rec = normalize_doc(doc)
                insert_doc(conn, rec)
                fetched += 1

                publishers = ensure_list(rec.get("publisher"))
                subjects = ensure_list(rec.get("subjects"))
                resource_groups = ensure_list(rec.get("resource_group"))
                formats = ensure_list(rec.get("format"))
                countries = ensure_list(rec.get("coverage_country"))

                publisher_counts.update(publishers)
                subject_counts.update(subjects)
                format_counts.update(formats)
                country_counts.update(countries)
                resource_group_counts.update(resource_groups)
                for group in resource_groups:
                    group_publishers[group].update(publishers)
                    group_subjects[group].update(subjects)
                    group_formats[group].update(formats)

            if fetched % 10000 == 0:
                conn.commit()
                print(f"  {fetched:,} / {total_to_fetch:,}")

            page_index += 1
            start = page_index * rows_per_page
            if start >= total_to_fetch:
                break
            if delay > 0:
                time.sleep(delay)
            current_page = query_page(query, start=start, rows=rows_per_page)
    finally:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

    def top_counts(counter, limit=25):
        return [{"name": n, "count": c} for n, c in counter.most_common(limit)]

    hierarchy = {
        "documents": fetched,
        "database": str(database_path),
        "top_level": {
            "resource_groups": top_counts(resource_group_counts),
            "publishers": top_counts(publisher_counts),
            "formats": top_counts(format_counts),
            "countries": top_counts(country_counts),
            "subjects": top_counts(subject_counts),
        },
        "resource_group_hierarchy": [
            {
                "name": group,
                "count": count,
                "top_publishers": top_counts(group_publishers[group]),
                "top_subjects": top_counts(group_subjects[group]),
                "top_formats": top_counts(group_formats[group]),
            }
            for group, count in resource_group_counts.most_common(25)
        ],
    }
    hierarchy_path = database_path.with_name("hsdl_catalog_hierarchy.json")
    hierarchy_path.write_text(json.dumps(hierarchy, indent=2), encoding="utf-8")

    return {"documents": fetched, "remote_total": total, "database": str(database_path)}


def incremental_sync(database_path, rows_per_page=500):
    query = get_public_documents_query()
    conn = sqlite3.connect(database_path)
    conn.execute("PRAGMA journal_mode=WAL")

    existing = set(r[0] for r in conn.execute("SELECT docid FROM documents").fetchall())

    added = 0
    start = 0

    try:
        while True:
            page = query_page(query, start=start, rows=rows_per_page)
            docs = page.get("docs", [])
            if not docs:
                break

            all_known = True
            for doc in docs:
                docid = doc.get("DocID")
                if docid in existing:
                    continue
                all_known = False
                formats = doc.get("Format")
                if isinstance(formats, list):
                    if "application/pdf" not in formats:
                        continue
                elif formats != "application/pdf":
                    continue
                rec = normalize_doc(doc)
                try:
                    insert_doc(conn, rec)
                    existing.add(docid)
                    added += 1
                except sqlite3.IntegrityError:
                    pass

            if all_known:
                break

            start += rows_per_page
            if start >= page.get("total", 0):
                break
    finally:
        conn.commit()
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.close()

    return {"added": added, "database": str(database_path)}


def main():
    parser = argparse.ArgumentParser(
        description="Scrape HSDL and build catalog database"
    )
    parser.add_argument("--database", default=settings.hsdl_db_path)
    parser.add_argument("--rows", type=int, default=10000)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--max-records", type=int)
    args = parser.parse_args()

    result = sync(
        database_path=Path(args.database),
        rows_per_page=args.rows,
        delay=args.delay,
        max_records=args.max_records,
    )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
