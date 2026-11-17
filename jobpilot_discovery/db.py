from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterable

from .models import Job
from .utils import fingerprint, normalize_url

APPLICATION_STATUSES = (
    "discovered", "shortlisted", "pending_review", "ready_to_apply", "applying",
    "submitted", "submission_unverified", "failed", "rejected", "interview",
    "assessment", "offer", "withdrawn",
)

APPLICATION_TRANSITIONS = {
    "discovered": {"shortlisted", "pending_review", "rejected", "withdrawn"},
    "shortlisted": {
        "pending_review", "ready_to_apply", "submitted", "interview", "assessment",
        "offer", "rejected", "withdrawn",
    },
    "pending_review": {
        "ready_to_apply", "applying", "submitted", "interview", "assessment",
        "offer", "failed", "rejected", "withdrawn",
    },
    "ready_to_apply": {
        "applying", "pending_review", "submitted", "interview", "assessment",
        "offer", "failed", "rejected", "withdrawn",
    },
    "applying": {
        "submitted", "submission_unverified", "failed", "pending_review",
        "interview", "assessment", "offer", "rejected",
    },
    "submitted": {"interview", "assessment", "offer", "rejected", "withdrawn"},
    "submission_unverified": {
        "submitted", "interview", "assessment", "offer", "rejected", "failed", "withdrawn",
    },
    "failed": {"pending_review", "submitted", "interview", "assessment", "offer", "rejected", "withdrawn"},
    "rejected": set(),
    "interview": {"assessment", "offer", "rejected", "withdrawn"},
    "assessment": {"interview", "offer", "rejected", "withdrawn"},
    "offer": {"withdrawn"},
    "withdrawn": set(),
}


class _ClosingConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc_value, traceback):
        try:
            return super().__exit__(exc_type, exc_value, traceback)
        finally:
            self.close()


class JobRepository:
    def __init__(self, db_path: Path):
        self.db_path = db_path
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=30, factory=_ClosingConnection)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    fingerprint TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    company TEXT NOT NULL,
                    location TEXT,
                    description TEXT NOT NULL DEFAULT '',
                    apply_url TEXT NOT NULL,
                    employment_type TEXT,
                    workplace_type TEXT,
                    posted_at TEXT,
                    department TEXT,
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    match_score INTEGER NOT NULL DEFAULT 0,
                    matched_role TEXT,
                    experience_status TEXT NOT NULL DEFAULT 'unknown',
                    match_reasons_json TEXT NOT NULL DEFAULT '[]',
                    status TEXT NOT NULL DEFAULT 'shortlisted',
                    first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS job_sources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    source TEXT NOT NULL,
                    source_job_id TEXT,
                    source_url TEXT NOT NULL,
                    raw_json TEXT NOT NULL DEFAULT '{}',
                    last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    UNIQUE(source, source_url)
                );
                CREATE TABLE IF NOT EXISTS source_state (
                    source TEXT PRIMARY KEY,
                    last_success_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_evaluation_cache (
                    cache_key TEXT PRIMARY KEY,
                    evaluation_json TEXT NOT NULL,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    name_key TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS ats_boards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
                    provider TEXT NOT NULL,
                    board_identifier TEXT NOT NULL,
                    career_page_url TEXT NOT NULL,
                    discovery_source TEXT NOT NULL,
                    discovered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    last_validated_at TEXT,
                    validation_status TEXT NOT NULL DEFAULT 'pending',
                    last_successful_fetch_at TEXT,
                    UNIQUE(provider, board_identifier)
                );
                CREATE TABLE IF NOT EXISTS discovery_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    status TEXT NOT NULL DEFAULT 'running',
                    queries_run INTEGER NOT NULL DEFAULT 0,
                    jobs_discovered INTEGER NOT NULL DEFAULT 0,
                    jobs_valid INTEGER NOT NULL DEFAULT 0,
                    duplicates INTEGER NOT NULL DEFAULT 0,
                    failed_requests INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                );
                CREATE TABLE IF NOT EXISTS search_queries (
                    query_key TEXT PRIMARY KEY,
                    provider TEXT NOT NULL,
                    query_text TEXT NOT NULL,
                    result_json TEXT NOT NULL,
                    executed_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS source_fetch_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    request_url TEXT NOT NULL,
                    status TEXT NOT NULL,
                    fetched_count INTEGER NOT NULL DEFAULT 0,
                    duration_ms INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS applications (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id INTEGER NOT NULL UNIQUE REFERENCES jobs(id) ON DELETE RESTRICT,
                    company_name TEXT NOT NULL,
                    company_key TEXT NOT NULL,
                    job_title TEXT NOT NULL,
                    title_key TEXT NOT NULL,
                    application_url TEXT NOT NULL,
                    canonical_url TEXT NOT NULL UNIQUE,
                    source TEXT,
                    source_job_id TEXT,
                    duplicate_confirmed INTEGER NOT NULL DEFAULT 0,
                    applied_at TEXT,
                    application_status TEXT NOT NULL DEFAULT 'shortlisted',
                    resume_used TEXT,
                    cover_letter_used TEXT,
                    submission_reference TEXT,
                    submission_verified INTEGER NOT NULL DEFAULT 0,
                    recruiter_email TEXT,
                    last_updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    notes TEXT,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_applications_status
                    ON applications(application_status, last_updated_at);
                CREATE INDEX IF NOT EXISTS idx_applications_company_title
                    ON applications(company_key, title_key);
                CREATE TABLE IF NOT EXISTS application_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    detail TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE TABLE IF NOT EXISTS recruiter_emails (
                    message_id TEXT PRIMARY KEY,
                    sender TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    received_at TEXT,
                    matched_application_id INTEGER REFERENCES applications(id) ON DELETE SET NULL,
                    classification TEXT NOT NULL,
                    confidence REAL NOT NULL DEFAULT 0,
                    processing_status TEXT NOT NULL DEFAULT 'processed',
                    summary TEXT,
                    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
                );
                CREATE INDEX IF NOT EXISTS idx_recruiter_emails_application
                    ON recruiter_emails(matched_application_id, received_at);
                CREATE TABLE IF NOT EXISTS agent_tasks (
                    task_id TEXT PRIMARY KEY,
                    task_type TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    started_at TEXT,
                    completed_at TEXT,
                    retry_count INTEGER NOT NULL DEFAULT 0,
                    max_retries INTEGER NOT NULL DEFAULT 3,
                    related_job_id INTEGER,
                    related_application_id INTEGER,
                    error_details TEXT,
                    payload_json TEXT NOT NULL DEFAULT '{}'
                );
                CREATE INDEX IF NOT EXISTS idx_agent_tasks_status
                    ON agent_tasks(status, created_at);
            """)
            migrations = {
                "match_score": "INTEGER NOT NULL DEFAULT 0",
                "matched_role": "TEXT",
                "experience_status": "TEXT NOT NULL DEFAULT 'unknown'",
                "match_reasons_json": "TEXT NOT NULL DEFAULT '[]'",
                "status": "TEXT NOT NULL DEFAULT 'shortlisted'",
                "canonical_url": "TEXT",
                "description_hash": "TEXT NOT NULL DEFAULT ''",
                "discovery_source": "TEXT",
                "eligibility_status": "TEXT NOT NULL DEFAULT 'pending'",
                "ai_evaluation_json": "TEXT",
                "missing_skills_json": "TEXT NOT NULL DEFAULT '[]'",
                "rejection_reason": "TEXT",
                "processing_status": "TEXT NOT NULL DEFAULT 'pending_ai'",
                "first_discovered_at": "TEXT",
                "last_updated_at": "TEXT",
            }
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
            for column, definition in migrations.items():
                if column not in existing:
                    conn.execute(f"ALTER TABLE jobs ADD COLUMN {column} {definition}")
            application_columns = {
                row["name"] for row in conn.execute("PRAGMA table_info(applications)")
            }
            if "duplicate_confirmed" not in application_columns:
                conn.execute(
                    "ALTER TABLE applications ADD COLUMN duplicate_confirmed INTEGER NOT NULL DEFAULT 0"
                )
            conn.execute("""
                UPDATE jobs SET first_discovered_at=COALESCE(first_discovered_at, first_seen_at),
                    last_updated_at=COALESCE(last_updated_at, last_seen_at),
                    eligibility_status=CASE WHEN status='shortlisted' AND eligibility_status='pending'
                        THEN 'eligible' ELSE eligibility_status END
            """)
            # Populate hashes for legacy rows using Python's stable SHA-256 implementation.
            rows = conn.execute("SELECT id, description FROM jobs WHERE description_hash=''").fetchall()
            for row in rows:
                digest = hashlib.sha256(row["description"].encode("utf-8")).hexdigest()
                conn.execute("UPDATE jobs SET description_hash=? WHERE id=?", (digest, row["id"]))
            conn.executescript("""
                CREATE INDEX IF NOT EXISTS idx_jobs_last_seen ON jobs(last_seen_at);
                CREATE INDEX IF NOT EXISTS idx_jobs_company_title ON jobs(company, title);
                CREATE INDEX IF NOT EXISTS idx_jobs_score ON jobs(match_score DESC);
                CREATE INDEX IF NOT EXISTS idx_jobs_canonical_url ON jobs(canonical_url);
                CREATE INDEX IF NOT EXISTS idx_jobs_processing ON jobs(processing_status, eligibility_status);
                CREATE INDEX IF NOT EXISTS idx_ats_boards_status ON ats_boards(validation_status, provider);
                CREATE INDEX IF NOT EXISTS idx_discovery_runs_started ON discovery_runs(started_at);
            """)

    def save_jobs(self, jobs: Iterable[Job]) -> dict[str, int]:
        counts = {"inserted": 0, "updated": 0}
        with self._connect() as conn:
            for job in jobs:
                apply_url = normalize_url(str(job.apply_url))
                source_url = normalize_url(str(job.source_url or job.apply_url))
                job_fp = fingerprint(job.title, job.company, job.location)
                description_hash = hashlib.sha256(job.description.encode("utf-8")).hexdigest()
                eligibility_status = "eligible" if job.eligibility_status == "pending" else job.eligibility_status
                processing_status = job.processing_status
                existing = None
                if job.source_job_id:
                    existing = conn.execute(
                        "SELECT job_id FROM job_sources WHERE source=? AND source_job_id=? ORDER BY id LIMIT 1",
                        (job.source, job.source_job_id),
                    ).fetchone()
                if existing is None:
                    existing = conn.execute(
                        "SELECT job_id FROM job_sources WHERE source=? AND source_url=?",
                        (job.source, source_url),
                    ).fetchone()
                if existing is None:
                    existing = conn.execute(
                        "SELECT id AS job_id FROM jobs WHERE canonical_url=? ORDER BY id LIMIT 1",
                        (apply_url,),
                    ).fetchone()
                if existing is None:
                    existing = conn.execute(
                        "SELECT id AS job_id FROM jobs WHERE fingerprint=?", (job_fp,)
                    ).fetchone()

                is_new = existing is None
                if is_new:
                    conn.execute("""
                        INSERT INTO jobs (fingerprint, title, company, location, description, apply_url,
                            employment_type, workplace_type, posted_at, department, tags_json,
                            match_score, matched_role, experience_status, match_reasons_json, status,
                            canonical_url, description_hash, discovery_source, eligibility_status,
                            rejection_reason, processing_status, first_discovered_at, last_updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    """, self._job_values(
                        job, job_fp, apply_url, description_hash, eligibility_status, processing_status
                    ))
                    job_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
                else:
                    job_id = existing["job_id"]
                    previous = conn.execute(
                        "SELECT description_hash, processing_status FROM jobs WHERE id=?", (job_id,)
                    ).fetchone()
                    changed_description = previous["description_hash"] != description_hash
                    if changed_description and eligibility_status == "eligible":
                        processing_status = "pending_ai"
                    conn.execute("""
                        UPDATE jobs SET title=?, company=?, location=?, description=?, apply_url=?,
                            employment_type=COALESCE(?, employment_type),
                            workplace_type=COALESCE(?, workplace_type),
                            posted_at=COALESCE(?, posted_at), department=COALESCE(?, department),
                            tags_json=?, match_score=?, matched_role=?, experience_status=?,
                            match_reasons_json=?, status=?, canonical_url=?, description_hash=?,
                            discovery_source=?, eligibility_status=?, rejection_reason=?,
                            processing_status=?, ai_evaluation_json=CASE WHEN ? THEN NULL ELSE ai_evaluation_json END,
                            missing_skills_json=CASE WHEN ? THEN '[]' ELSE missing_skills_json END,
                            last_seen_at=CURRENT_TIMESTAMP, last_updated_at=CURRENT_TIMESTAMP
                        WHERE id=?
                    """, (
                        job.title, job.company, job.location, job.description, apply_url,
                        job.employment_type, job.workplace_type,
                        job.posted_at.isoformat() if job.posted_at else None, job.department,
                        json.dumps(job.tags), job.match_score, job.matched_role,
                        job.experience_status, json.dumps(job.match_reasons),
                        "shortlisted" if eligibility_status == "eligible" else "rejected",
                        apply_url, description_hash, job.source, eligibility_status,
                        job.rejection_reason, processing_status, changed_description,
                        changed_description, job_id,
                    ))

                raw_json = json.dumps(job.raw_data, ensure_ascii=False, default=str)
                source_row = conn.execute(
                    "SELECT id FROM job_sources WHERE source=? AND source_url=?",
                    (job.source, source_url),
                ).fetchone()
                if source_row:
                    conn.execute("""
                        UPDATE job_sources SET job_id=?, source_job_id=?, raw_json=?,
                            last_seen_at=CURRENT_TIMESTAMP WHERE id=?
                    """, (job_id, job.source_job_id, raw_json, source_row["id"]))
                else:
                    conn.execute("""
                        INSERT OR IGNORE INTO job_sources
                            (job_id, source, source_job_id, source_url, raw_json)
                        VALUES (?, ?, ?, ?, ?)
                    """, (job_id, job.source, job.source_job_id, source_url, raw_json))
                counts["inserted" if is_new else "updated"] += 1
        return counts

    @staticmethod
    def _job_values(
        job: Job,
        job_fp: str,
        apply_url: str,
        description_hash: str,
        eligibility_status: str,
        processing_status: str,
    ) -> tuple:
        return (
            job_fp, job.title, job.company, job.location, job.description, apply_url,
            job.employment_type, job.workplace_type,
            job.posted_at.isoformat() if job.posted_at else None, job.department,
            json.dumps(job.tags), job.match_score, job.matched_role, job.experience_status,
            json.dumps(job.match_reasons),
            "shortlisted" if eligibility_status == "eligible" else "rejected",
            apply_url, description_hash, job.source, eligibility_status,
            job.rejection_reason, processing_status,
        )

    def mark_source_success(self, source: str, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO source_state(source,last_success_at) VALUES(?,?)
                ON CONFLICT(source) DO UPDATE SET last_success_at=excluded.last_success_at
            """, (source, timestamp))

    def source_last_success(self, source: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("SELECT last_success_at FROM source_state WHERE source=?", (source,)).fetchone()
            return row[0] if row else None

    def count_jobs(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM jobs WHERE status='shortlisted'").fetchone()[0])

    def recent_jobs(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT jobs.*,
                    (SELECT source FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source,
                    (SELECT source_url FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source_url
                FROM jobs WHERE status='shortlisted'
                ORDER BY match_score DESC, last_seen_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def rejected_jobs(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT jobs.*,
                    (SELECT source FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source,
                    (SELECT source_url FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source_url
                FROM jobs WHERE eligibility_status='rejected'
                ORDER BY last_seen_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def pending_ai_jobs(self, limit: int = 100) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT jobs.*,
                    (SELECT source FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source,
                    (SELECT source_url FROM job_sources WHERE job_id=jobs.id ORDER BY id LIMIT 1) AS source_url
                FROM jobs
                WHERE eligibility_status='eligible' AND processing_status='pending_ai'
                ORDER BY last_seen_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    def update_ai_evaluation(
        self, job_id: int, evaluation: dict, score: int, missing_skills: list[str]
    ) -> None:
        rejected = evaluation["decision"] == "REJECT"
        with self._connect() as conn:
            conn.execute("""
                UPDATE jobs SET ai_evaluation_json=?, match_score=?, missing_skills_json=?,
                    eligibility_status=CASE WHEN ? THEN 'rejected' ELSE eligibility_status END,
                    rejection_reason=CASE WHEN ? THEN ? ELSE rejection_reason END,
                    status=CASE WHEN ? THEN 'rejected' ELSE status END,
                    processing_status='evaluated', last_updated_at=CURRENT_TIMESTAMP
                WHERE id=?
            """, (
                json.dumps(evaluation, ensure_ascii=False), score,
                json.dumps(missing_skills, ensure_ascii=False), rejected, rejected,
                evaluation["reason"], rejected, job_id,
            ))

    def get_cached_evaluation(self, cache_key: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT evaluation_json FROM ai_evaluation_cache WHERE cache_key=?", (cache_key,)
            ).fetchone()
            return json.loads(row["evaluation_json"]) if row else None

    def cache_evaluation(self, cache_key: str, evaluation: dict) -> None:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO ai_evaluation_cache(cache_key, evaluation_json) VALUES(?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET evaluation_json=excluded.evaluation_json
            """, (cache_key, json.dumps(evaluation, ensure_ascii=False)))

    def source_stats(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT js.source, COUNT(DISTINCT j.id) AS total,
                    SUM(CASE WHEN j.eligibility_status='eligible' THEN 1 ELSE 0 END) AS relevant,
                    SUM(CASE WHEN j.eligibility_status='rejected' THEN 1 ELSE 0 END) AS rejected
                FROM job_sources js JOIN jobs j ON j.id=js.job_id
                GROUP BY js.source ORDER BY js.source
            """).fetchall()
            return [dict(row) for row in rows]

    def count_by_status(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT processing_status, COUNT(*) AS total FROM jobs GROUP BY processing_status
            """).fetchall()
            return {row["processing_status"]: row["total"] for row in rows}

    def save_company_board(
        self,
        *,
        company_name: str,
        provider: str,
        board_identifier: str,
        career_page_url: str,
        discovery_source: str,
        validation_status: str,
        last_validated_at: str | None = None,
    ) -> None:
        company_key = " ".join(company_name.casefold().split())
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO companies(name, name_key) VALUES(?, ?)
                ON CONFLICT(name_key) DO UPDATE SET
                    name=excluded.name, updated_at=CURRENT_TIMESTAMP
            """, (company_name.strip(), company_key))
            company_id = conn.execute(
                "SELECT id FROM companies WHERE name_key=?", (company_key,)
            ).fetchone()["id"]
            conn.execute("""
                INSERT INTO ats_boards(
                    company_id, provider, board_identifier, career_page_url, discovery_source,
                    last_validated_at, validation_status
                ) VALUES(?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(provider, board_identifier) DO UPDATE SET
                    company_id=excluded.company_id,
                    career_page_url=excluded.career_page_url,
                    discovery_source=excluded.discovery_source,
                    last_validated_at=COALESCE(excluded.last_validated_at, ats_boards.last_validated_at),
                    validation_status=excluded.validation_status
            """, (
                company_id, provider, board_identifier, career_page_url, discovery_source,
                last_validated_at, validation_status,
            ))

    def company_boards(self, provider: str | None = None) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.name AS company_name, b.provider, b.board_identifier,
                    b.career_page_url, b.discovery_source, b.discovered_at,
                    b.last_validated_at, b.validation_status, b.last_successful_fetch_at
                FROM ats_boards b JOIN companies c ON c.id=b.company_id
                WHERE (? IS NULL OR b.provider=?) ORDER BY c.name, b.provider
            """, (provider, provider)).fetchall()
            return [dict(row) for row in rows]

    def companies(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT c.name, COUNT(b.id) AS board_count,
                    SUM(CASE WHEN b.validation_status='valid' THEN 1 ELSE 0 END) AS valid_boards
                FROM companies c LEFT JOIN ats_boards b ON b.company_id=c.id
                GROUP BY c.id ORDER BY c.name
            """).fetchall()
            return [dict(row) for row in rows]

    def valid_ats_boards(self, provider: str | None = None) -> list[dict]:
        return [
            board for board in self.company_boards(provider)
            if board["validation_status"] == "valid"
        ]

    def mark_board_fetch(self, provider: str, identifier: str, timestamp: str) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE ats_boards SET last_successful_fetch_at=?
                WHERE provider=? AND board_identifier=?
            """, (timestamp, provider, identifier))

    def get_search_query_cache(self, provider: str, query: str) -> list[dict] | None:
        key = hashlib.sha256(f"{provider}\n{query.casefold().strip()}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT result_json, expires_at FROM search_queries WHERE query_key=?", (key,)
            ).fetchone()
            if row is None:
                return None
            expires_at = datetime.fromisoformat(row["expires_at"])
            if expires_at.tzinfo is None:
                expires_at = expires_at.replace(tzinfo=timezone.utc)
            if expires_at <= datetime.now(timezone.utc):
                return None
            return json.loads(row["result_json"])

    def set_search_query_cache(
        self, provider: str, query: str, results: list[dict], ttl_hours: int
    ) -> None:
        now = datetime.now(timezone.utc)
        key = hashlib.sha256(f"{provider}\n{query.casefold().strip()}".encode("utf-8")).hexdigest()
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO search_queries(query_key, provider, query_text, result_json, executed_at, expires_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(query_key) DO UPDATE SET
                    result_json=excluded.result_json,
                    executed_at=excluded.executed_at,
                    expires_at=excluded.expires_at
            """, (
                key, provider, query, json.dumps(results, ensure_ascii=False, default=str),
                now.isoformat(), (now + timedelta(hours=ttl_hours)).isoformat(),
            ))

    def start_discovery_run(self, source: str, queries_run: int = 0) -> int:
        with self._connect() as conn:
            conn.execute("""
                INSERT INTO discovery_runs(source, started_at, queries_run)
                VALUES(?, ?, ?)
            """, (source, datetime.now(timezone.utc).isoformat(), queries_run))
            return int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])

    def finish_discovery_run(
        self,
        run_id: int,
        *,
        status: str,
        jobs_discovered: int = 0,
        jobs_valid: int = 0,
        duplicates: int = 0,
        failed_requests: int = 0,
        error: str | None = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute("""
                UPDATE discovery_runs SET completed_at=?, status=?, jobs_discovered=?,
                    jobs_valid=?, duplicates=?, failed_requests=?, error=? WHERE id=?
            """, (
                datetime.now(timezone.utc).isoformat(), status, jobs_discovered, jobs_valid,
                duplicates, failed_requests, error, run_id,
            ))

    def discovery_stats(self, limit: int = 20) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT source, COUNT(*) AS runs,
                    SUM(jobs_discovered) AS jobs_discovered,
                    SUM(jobs_valid) AS jobs_valid,
                    SUM(duplicates) AS duplicates,
                    SUM(failed_requests) AS failed_requests,
                    MAX(completed_at) AS last_completed_at
                FROM discovery_runs GROUP BY source ORDER BY source LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

    @staticmethod
    def _application_key(value: str) -> str:
        return " ".join(re.sub(r"[^a-z0-9]+", " ", value.casefold()).split())

    def create_application(
        self, job_id: int, *, confirm_possible_duplicate: bool = False
    ) -> dict:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            job = conn.execute("""
                SELECT j.id, j.title, j.company, j.apply_url,
                    (SELECT source FROM job_sources WHERE job_id=j.id ORDER BY id LIMIT 1) AS source,
                    (SELECT source_job_id FROM job_sources WHERE job_id=j.id ORDER BY id LIMIT 1)
                        AS source_job_id
                FROM jobs j WHERE j.id=?
            """, (job_id,)).fetchone()
            if job is None:
                raise KeyError(f"Job {job_id} does not exist")

            canonical_url = normalize_url(job["apply_url"])
            existing = conn.execute("""
                SELECT id FROM applications WHERE job_id=? OR canonical_url=?
                ORDER BY id LIMIT 1
            """, (job_id, canonical_url)).fetchone()
            if existing is not None:
                return {
                    "created": False,
                    "application_id": existing["id"],
                    "duplicate": True,
                    "possible_duplicate_ids": [],
                }

            company_key = self._application_key(job["company"])
            title_key = self._application_key(job["title"])
            possible_duplicates = conn.execute("""
                SELECT id FROM applications WHERE company_key=? AND title_key=? ORDER BY id
            """, (company_key, title_key)).fetchall()
            possible_ids = [row["id"] for row in possible_duplicates]
            if possible_ids and not confirm_possible_duplicate:
                return {
                    "created": False,
                    "application_id": None,
                    "duplicate": False,
                    "possible_duplicate_ids": possible_ids,
                }

            conn.execute("""
                INSERT INTO applications(
                    job_id, company_name, company_key, job_title, title_key,
                    application_url, canonical_url, source, source_job_id,
                    duplicate_confirmed, application_status
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'shortlisted')
            """, (
                job["id"], job["company"], company_key, job["title"], title_key,
                job["apply_url"], canonical_url, job["source"], job["source_job_id"],
                int(bool(possible_ids and confirm_possible_duplicate)),
            ))
            application_id = int(conn.execute("SELECT last_insert_rowid()").fetchone()[0])
            conn.execute("""
                INSERT INTO application_events(application_id, event_type, detail)
                VALUES(?, 'created', 'Application tracking record created')
            """, (application_id,))
            return {
                "created": True,
                "application_id": application_id,
                "duplicate": False,
                "possible_duplicate_ids": possible_ids,
            }

    def applications(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        if status is not None and status not in APPLICATION_STATUSES:
            raise ValueError(f"Unsupported application status: {status}")
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT a.*, j.location, j.match_score, j.eligibility_status,
                    j.processing_status, j.ai_evaluation_json
                FROM applications a JOIN jobs j ON j.id=a.job_id
                WHERE (? IS NULL OR a.application_status=?)
                ORDER BY a.last_updated_at DESC, a.id DESC LIMIT ?
            """, (status, status, limit)).fetchall()
            return [dict(row) for row in rows]

    def application(self, application_id: int) -> dict | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT a.*, j.location, j.match_score, j.eligibility_status,
                    j.processing_status, j.description, j.ai_evaluation_json
                FROM applications a JOIN jobs j ON j.id=a.job_id
                WHERE a.id=?
            """, (application_id,)).fetchone()
            if row is None:
                return None
            result = dict(row)
            result["events"] = [
                dict(event) for event in conn.execute("""
                    SELECT event_type, detail, created_at FROM application_events
                    WHERE application_id=? ORDER BY id
                """, (application_id,)).fetchall()
            ]
            return result

    def update_application_status(
        self,
        application_id: int,
        status: str,
        *,
        submission_verified: bool = False,
        submission_reference: str | None = None,
        error_message: str | None = None,
        detail: str | None = None,
    ) -> None:
        if status not in APPLICATION_STATUSES:
            raise ValueError(f"Unsupported application status: {status}")
        if status == "submitted" and not submission_verified:
            raise ValueError("An application can be marked submitted only after confirmation")
        if status == "submission_unverified" and submission_verified:
            raise ValueError("An unverified submission cannot be marked verified")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            current = conn.execute("""
                SELECT application_status FROM applications WHERE id=?
            """, (application_id,)).fetchone()
            if current is None:
                raise KeyError(f"Application {application_id} does not exist")
            old_status = current["application_status"]
            if status != old_status and status not in APPLICATION_TRANSITIONS[old_status]:
                raise ValueError(f"Invalid application status transition: {old_status} -> {status}")
            conn.execute("""
                UPDATE applications SET application_status=?,
                    submission_verified=CASE WHEN ?='submitted' THEN ? ELSE submission_verified END,
                    submission_reference=COALESCE(?, submission_reference),
                    applied_at=CASE WHEN ?='submitted' THEN COALESCE(applied_at, ?) ELSE applied_at END,
                    error_message=COALESCE(?, error_message),
                    last_updated_at=? WHERE id=?
            """, (
                status, status, int(submission_verified), submission_reference, status,
                datetime.now(timezone.utc).isoformat(), error_message,
                datetime.now(timezone.utc).isoformat(), application_id,
            ))
            conn.execute("""
                INSERT INTO application_events(application_id, event_type, detail)
                VALUES(?, ?, ?)
            """, (application_id, status, detail or f"Status changed from {old_status} to {status}"))

    def retry_application(self, application_id: int) -> None:
        application = self.application(application_id)
        if application is None:
            raise KeyError(f"Application {application_id} does not exist")
        if application["application_status"] != "failed":
            raise ValueError("Only confirmed failed applications can be retried")
        if application["submission_verified"]:
            raise ValueError("A submitted application cannot be retried")
        if self.has_prior_application_attempt(application_id):
            raise ValueError("A prior submit attempt must be manually verified before retrying")
        self.update_application_status(
            application_id, "pending_review",
            detail="Retry requested; manual review is required before another attempt",
        )

    def application_stats(self) -> dict[str, int]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT application_status, COUNT(*) AS total
                FROM applications GROUP BY application_status
            """).fetchall()
            return {row["application_status"]: row["total"] for row in rows}

    def application_export_rows(self) -> list[dict]:
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT id, job_id, company_name, job_title, application_url, source,
                    applied_at, application_status, resume_used, cover_letter_used,
                    submission_reference, submission_verified, recruiter_email,
                    last_updated_at, notes, error_message
                FROM applications ORDER BY id
            """).fetchall()
            return [dict(row) for row in rows]

    def add_application_event(
        self, application_id: int, event_type: str, detail: str | None = None
    ) -> None:
        with self._connect() as conn:
            exists = conn.execute(
                "SELECT 1 FROM applications WHERE id=?", (application_id,)
            ).fetchone()
            if exists is None:
                raise KeyError(f"Application {application_id} does not exist")
            conn.execute("""
                INSERT INTO application_events(application_id, event_type, detail)
                VALUES(?, ?, ?)
            """, (application_id, event_type, detail))

    def record_application_materials(
        self,
        application_id: int,
        *,
        resume_used: str | None = None,
        cover_letter_used: str | None = None,
    ) -> None:
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE applications SET resume_used=COALESCE(?, resume_used),
                    cover_letter_used=COALESCE(?, cover_letter_used),
                    last_updated_at=? WHERE id=?
            """, (
                resume_used, cover_letter_used,
                datetime.now(timezone.utc).isoformat(), application_id,
            ))
            if cursor.rowcount == 0:
                raise KeyError(f"Application {application_id} does not exist")

    def has_prior_application_attempt(self, application_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT 1 FROM application_events
                WHERE application_id=? AND event_type IN (
                    'submission_attempt_started', 'submitted', 'submission_unverified'
                ) LIMIT 1
            """, (application_id,)).fetchone()
            return row is not None

    def has_other_company_title_application(self, application_id: int) -> bool:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT 1 FROM applications current
                JOIN applications other
                  ON other.company_key=current.company_key AND other.title_key=current.title_key
                WHERE current.id=? AND other.id<>current.id
                LIMIT 1
            """, (application_id,)).fetchone()
            return row is not None

    def application_limits_available(
        self,
        company_name: str,
        *,
        max_per_day: int,
        max_per_company_per_day: int,
    ) -> bool:
        midnight = datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0
        ).isoformat()
        company_key = self._application_key(company_name)
        with self._connect() as conn:
            counts = conn.execute("""
                SELECT COUNT(DISTINCT a.id) AS total,
                    COUNT(DISTINCT CASE WHEN a.company_key=? THEN a.id END) AS company_total
                FROM applications a JOIN application_events e ON e.application_id=a.id
                WHERE e.created_at>=? AND e.event_type IN (
                    'submission_attempt_started', 'submitted', 'submission_unverified'
                )
            """, (company_key, midnight)).fetchone()
            return (
                counts["total"] < max_per_day
                and counts["company_total"] < max_per_company_per_day
            )

    def save_recruiter_email(
        self,
        *,
        message_id: str,
        sender: str,
        subject: str,
        received_at: str | None,
        matched_application_id: int | None,
        classification: str,
        confidence: float,
        processing_status: str,
        summary: str,
        update_application_to: str | None = None,
    ) -> bool:
        if not 0 <= confidence <= 1:
            raise ValueError("email classification confidence must be between 0 and 1")
        if update_application_to is not None and update_application_to not in APPLICATION_STATUSES:
            raise ValueError(f"Unsupported application status: {update_application_to}")
        if update_application_to == "submitted" and (
            classification != "application_confirmation" or confidence < 0.95
        ):
            raise ValueError("Only a high-confidence application confirmation can verify submission")

        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if conn.execute(
                "SELECT 1 FROM recruiter_emails WHERE message_id=?", (message_id,)
            ).fetchone():
                return False

            effective_status = processing_status
            current_application = None
            if matched_application_id is not None:
                current_application = conn.execute("""
                    SELECT application_status FROM applications WHERE id=?
                """, (matched_application_id,)).fetchone()
                if current_application is None:
                    matched_application_id = None
                    effective_status = "unmatched"
                elif update_application_to is not None:
                    old_status = current_application["application_status"]
                    if (
                        update_application_to != old_status
                        and update_application_to not in APPLICATION_TRANSITIONS[old_status]
                    ):
                        update_application_to = None
                        effective_status = "review"

            conn.execute("""
                INSERT INTO recruiter_emails(
                    message_id, sender, subject, received_at, matched_application_id,
                    classification, confidence, processing_status, summary
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                message_id, sender, subject, received_at, matched_application_id,
                classification, confidence, effective_status, summary,
            ))

            if matched_application_id is not None and current_application is not None:
                if update_application_to is not None:
                    now = datetime.now(timezone.utc).isoformat()
                    conn.execute("""
                        UPDATE applications SET application_status=?,
                            submission_verified=CASE WHEN ?='submitted' THEN 1 ELSE submission_verified END,
                            applied_at=CASE WHEN ?='submitted' THEN COALESCE(applied_at, ?) ELSE applied_at END,
                            last_updated_at=? WHERE id=?
                    """, (
                        update_application_to, update_application_to,
                        update_application_to, now, now, matched_application_id,
                    ))
                conn.execute("""
                    INSERT INTO application_events(application_id, event_type, detail)
                    VALUES(?, ?, ?)
                """, (
                    matched_application_id,
                    f"email:{classification}",
                    summary,
                ))
            return True

    def recruiter_emails(
        self, *, processing_status: str | None = None, limit: int = 100
    ) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT e.*, a.company_name, a.job_title
                FROM recruiter_emails e
                LEFT JOIN applications a ON a.id=e.matched_application_id
                WHERE (? IS NULL OR e.processing_status=?)
                ORDER BY e.received_at DESC, e.created_at DESC LIMIT ?
            """, (processing_status, processing_status, limit)).fetchall()
            return [dict(row) for row in rows]

    def create_agent_task(
        self,
        task_type: str,
        dedupe_key: str,
        *,
        max_retries: int = 3,
        related_job_id: int | None = None,
        related_application_id: int | None = None,
        payload: dict | None = None,
    ) -> tuple[str, bool]:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            active = conn.execute("""
                SELECT task_id FROM agent_tasks
                WHERE task_type=? AND dedupe_key=? AND status IN ('pending', 'running')
                ORDER BY created_at DESC LIMIT 1
            """, (task_type, dedupe_key)).fetchone()
            if active is not None:
                return active["task_id"], False
            task_id = str(uuid.uuid4())
            conn.execute("""
                INSERT INTO agent_tasks(
                    task_id, task_type, dedupe_key, status, created_at, max_retries,
                    related_job_id, related_application_id, payload_json
                ) VALUES(?, ?, ?, 'pending', ?, ?, ?, ?, ?)
            """, (
                task_id, task_type, dedupe_key, datetime.now(timezone.utc).isoformat(),
                max_retries, related_job_id, related_application_id,
                json.dumps(payload or {}, ensure_ascii=False),
            ))
            return task_id, True

    def claim_agent_task(self, task_id: str) -> bool:
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute("""
                UPDATE agent_tasks SET status='running', started_at=?, error_details=NULL
                WHERE task_id=? AND status='pending'
            """, (datetime.now(timezone.utc).isoformat(), task_id))
            if cursor.rowcount:
                return True
            if conn.execute(
                "SELECT 1 FROM agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone() is None:
                raise KeyError(f"Agent task {task_id} does not exist")
            return False

    def finish_agent_task(
        self, task_id: str, *, status: str, error_details: str | None = None
    ) -> None:
        if status not in {"completed", "failed", "requires_manual_review"}:
            raise ValueError("Agent task completion status is invalid")
        with self._connect() as conn:
            cursor = conn.execute("""
                UPDATE agent_tasks SET status=?, completed_at=?, error_details=?,
                    retry_count=retry_count+CASE WHEN ?='failed' THEN 1 ELSE 0 END
                WHERE task_id=? AND status='running'
            """, (
                status, datetime.now(timezone.utc).isoformat(), error_details, status, task_id,
            ))
            if cursor.rowcount == 0:
                raise ValueError(f"Agent task {task_id} is not running")

    def recover_agent_tasks(self) -> dict[str, int]:
        now = datetime.now(timezone.utc).isoformat()
        counts = {"rescheduled": 0, "failed": 0, "manual_review": 0}
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            tasks = conn.execute(
                "SELECT * FROM agent_tasks WHERE status='running'"
            ).fetchall()
            for task in tasks:
                retry_count = task["retry_count"] + 1
                if task["task_type"] == "application_submission":
                    application_id = task["related_application_id"]
                    verified_application = False
                    if application_id is not None:
                        app = conn.execute("""
                            SELECT application_status, submission_verified
                            FROM applications WHERE id=?
                        """, (application_id,)).fetchone()
                        verified_application = bool(
                            app is not None
                            and app["submission_verified"]
                            and app["application_status"] in {
                                "submitted", "interview", "assessment", "offer",
                                "rejected", "withdrawn",
                            }
                        )
                        if app is not None and app["application_status"] == "applying":
                            conn.execute("""
                                UPDATE applications SET application_status='submission_unverified',
                                    submission_verified=0, applied_at=COALESCE(applied_at, ?),
                                    error_message='Agent stopped during submission; manual verification required',
                                    last_updated_at=? WHERE id=?
                            """, (now, now, application_id))
                            conn.execute("""
                                INSERT INTO application_events(application_id, event_type, detail)
                                VALUES(?, 'submission_unverified',
                                    'Agent recovery marked an interrupted submission for manual verification')
                            """, (application_id,))
                    if verified_application:
                        conn.execute("""
                            UPDATE agent_tasks SET status='completed', completed_at=?,
                                error_details=NULL WHERE task_id=?
                        """, (now, task["task_id"]))
                    else:
                        conn.execute("""
                            UPDATE agent_tasks SET status='requires_manual_review',
                                completed_at=?, retry_count=?, error_details=?
                            WHERE task_id=?
                        """, (
                            now, retry_count,
                            "Interrupted application submission; never retried automatically",
                            task["task_id"],
                        ))
                        counts["manual_review"] += 1
                elif retry_count <= task["max_retries"]:
                    conn.execute("""
                        UPDATE agent_tasks SET status='pending', started_at=NULL,
                            completed_at=NULL, retry_count=?, error_details=?
                        WHERE task_id=?
                    """, (
                        retry_count, "Interrupted task queued for bounded retry", task["task_id"],
                    ))
                    counts["rescheduled"] += 1
                else:
                    conn.execute("""
                        UPDATE agent_tasks SET status='failed', completed_at=?,
                            retry_count=?, error_details=?
                        WHERE task_id=?
                    """, (
                        now, retry_count, "Interrupted task exceeded its retry limit",
                        task["task_id"],
                    ))
                    counts["failed"] += 1
            orphaned = conn.execute("""
                SELECT a.id FROM applications a
                WHERE a.application_status='applying'
                    AND NOT EXISTS (
                        SELECT 1 FROM agent_tasks t
                        WHERE t.related_application_id=a.id AND t.status='running'
                    )
            """).fetchall()
            for application in orphaned:
                conn.execute("""
                    UPDATE applications SET application_status='submission_unverified',
                        submission_verified=0, applied_at=COALESCE(applied_at, ?),
                        error_message='Interrupted submission requires manual verification',
                        last_updated_at=? WHERE id=?
                """, (now, now, application["id"]))
                conn.execute("""
                    INSERT INTO application_events(application_id, event_type, detail)
                    VALUES(?, 'submission_unverified',
                        'Recovery marked an orphaned submission for manual verification')
                """, (application["id"],))
                counts["manual_review"] += 1
        return counts

    def agent_tasks(self, *, status: str | None = None, limit: int = 100) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT * FROM agent_tasks WHERE (? IS NULL OR status=?)
                ORDER BY created_at DESC LIMIT ?
            """, (status, status, limit)).fetchall()
            return [dict(row) for row in rows]

    def agent_task(self, task_id: str) -> dict | None:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM agent_tasks WHERE task_id=?", (task_id,)
            ).fetchone()
            return dict(row) if row else None

    def last_agent_task_completion(self, task_type: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT MAX(completed_at) FROM agent_tasks
                WHERE task_type=? AND status='completed'
            """, (task_type,)).fetchone()
            return row[0] if row else None

    def last_agent_task_attempt(self, task_type: str) -> str | None:
        with self._connect() as conn:
            row = conn.execute("""
                SELECT CASE
                    WHEN EXISTS (
                        SELECT 1 FROM agent_tasks
                        WHERE task_type=? AND status='pending'
                    ) THEN NULL
                    ELSE (
                        SELECT MAX(COALESCE(completed_at, started_at, created_at))
                        FROM agent_tasks WHERE task_type=?
                    )
                END
            """, (task_type, task_type)).fetchone()
            return row[0] if row else None

    def eligible_jobs_without_applications(self, limit: int = 100) -> list[dict]:
        if limit < 1:
            raise ValueError("limit must be at least 1")
        with self._connect() as conn:
            rows = conn.execute("""
                SELECT j.id, j.title, j.company, j.match_score
                FROM jobs j
                WHERE j.eligibility_status='eligible'
                  AND NOT EXISTS (
                    SELECT 1 FROM applications a
                    WHERE a.job_id=j.id OR a.canonical_url=j.canonical_url
                  )
                ORDER BY j.match_score DESC, j.last_seen_at DESC LIMIT ?
            """, (limit,)).fetchall()
            return [dict(row) for row in rows]

