# HSDL Document Explorer

FastAPI backend that indexes the Homeland Security Digital Library catalog into SQLite and serves it as an [OpenBB Workspace](https://pro.openbb.co) backend.

The database is built at Docker build time. A background task checks for new documents every 12 hours and adds them incrementally.

## Local development

```bash
conda create -n hsdl python=3.12 -y
conda activate hsdl

pip install -e .

# sync catalog (builds the SQLite database)
python -m src.sync

# run the server
uvicorn src.main:app --host 0.0.0.0 --port 7780 --reload
```

The API will be available at `http://localhost:7780`. The sync step scrapes the HSDL public catalog and builds a SQLite database with FTS5 full-text search.

### Environment variables

| Variable | Default | Description |
|---|---|---|
| `HSDL_DB_PATH` | `data/hsdl_catalog.db` | Path to the SQLite file |
| `HSDL_DATA_DIR` | `data` | Directory for data files |
| `PORT` | `7780` | Server port |

## Docker

```bash
docker compose up --build
```

The Docker entry runs `python -m src.sync`. A background task runs an incremental sync every 12 hours to pick up new documents.

Data persists in the `hsdl_data` volume. A check for new documents occurs nightly.

## Deployment

Deployment is automated via GitHub Actions using [Dokku](https://dokku.com). Pushing to `main` triggers a deploy.
