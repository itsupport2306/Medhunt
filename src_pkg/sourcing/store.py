"""
Database store for candidates, jobs, pipeline, outreach, and do-not-contact.

Neon/PostgreSQL is used when DATABASE_URL is configured. SQLite remains the
zero-configuration fallback for local demos and tests.
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from contextlib import contextmanager
from pathlib import Path

from . import config

PIPELINE_STAGES = ["new", "enriched", "contacted", "replied", "submitted", "rejected"]

_SQLITE_SCHEMA = """
CREATE TABLE IF NOT EXISTS users(
  id INTEGER PRIMARY KEY AUTOINCREMENT, auth0_sub TEXT UNIQUE NOT NULL,
  email TEXT DEFAULT '', name TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS enrichment_events(
  id INTEGER PRIMARY KEY AUTOINCREMENT, auth0_sub TEXT NOT NULL,
  candidate_id INTEGER NOT NULL, status TEXT NOT NULL, provider TEXT DEFAULT '',
  run_id TEXT DEFAULT '', created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS jobs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, title TEXT, location TEXT,
  description TEXT, created REAL);
CREATE TABLE IF NOT EXISTS candidates(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT, location TEXT,
  hometown TEXT DEFAULT '',
  job_id INTEGER, stage TEXT DEFAULT 'new', fit_score REAL DEFAULT 0,
  phones TEXT DEFAULT '[]', emails TEXT DEFAULT '[]', addresses TEXT DEFAULT '[]',
  enrich_status TEXT DEFAULT 'pending', confidence REAL DEFAULT 0,
  verification TEXT DEFAULT '{}',
  canonical_name TEXT DEFAULT '', identity_status TEXT DEFAULT 'captured',
  identity_score REAL DEFAULT 0, identity_provider TEXT DEFAULT '',
  provider_person_id TEXT DEFAULT '', master_candidate_id INTEGER,
  identity_evidence TEXT DEFAULT '{}',
  identity_verified_at REAL DEFAULT 0, contact_verified_at REAL DEFAULT 0,
  contact_expires_at REAL DEFAULT 0,
  notes TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
  source_id TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS medhunt_ats_routes(
  candidate_id INTEGER NOT NULL, user_id TEXT NOT NULL,
  destination TEXT NOT NULL DEFAULT 'nexus', eligibility TEXT DEFAULT '{}',
  updated REAL NOT NULL, PRIMARY KEY(candidate_id,user_id));
CREATE TABLE IF NOT EXISTS outreach(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER, channel TEXT,
  subject TEXT, body TEXT, status TEXT DEFAULT 'draft', created REAL);
CREATE TABLE IF NOT EXISTS sms_consents(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  phone_key TEXT NOT NULL, phone TEXT NOT NULL, status TEXT NOT NULL,
  source TEXT NOT NULL, evidence TEXT DEFAULT '', disclosure_version TEXT DEFAULT 'v1',
  captured_by TEXT DEFAULT '', captured_at REAL, created REAL, updated REAL,
  UNIQUE(candidate_id, phone_key));
CREATE TABLE IF NOT EXISTS sms_conversations(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  nexus_candidate_id TEXT DEFAULT '', candidate_name TEXT DEFAULT '',
  candidate_phone TEXT NOT NULL, phone_key TEXT NOT NULL,
  initiated_by TEXT DEFAULT '', assigned_recruiter_id TEXT DEFAULT '',
  assigned_recruiter_email TEXT DEFAULT '', assigned_recruiter_name TEXT DEFAULT '',
  zoom_sender_number TEXT DEFAULT '', zoom_sender_user_id TEXT DEFAULT '',
  zoom_session_id TEXT DEFAULT '',
  status TEXT DEFAULT 'open', created REAL, updated REAL, last_message_at REAL);
CREATE TABLE IF NOT EXISTS sms_messages(
  id INTEGER PRIMARY KEY AUTOINCREMENT, conversation_id INTEGER NOT NULL,
  direction TEXT NOT NULL, body TEXT NOT NULL, status TEXT DEFAULT 'queued',
  zoom_message_id TEXT DEFAULT '', request_id TEXT UNIQUE,
  sender_user_id TEXT DEFAULT '', sender_name TEXT DEFAULT '',
  sender_number TEXT DEFAULT '', zoom_user_id TEXT DEFAULT '',
  failure_reason TEXT DEFAULT '', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS sms_webhook_events(
  event_key TEXT PRIMARY KEY, event_type TEXT NOT NULL, created REAL);
CREATE TABLE IF NOT EXISTS sms_outreach_claims(
  candidate_id INTEGER PRIMARY KEY, phone_key TEXT NOT NULL,
  status TEXT DEFAULT 'sending', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS talent_pools(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT UNIQUE NOT NULL,
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS talent_pool_members(
  pool_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL, created REAL,
  UNIQUE(pool_id, candidate_id));
CREATE TABLE IF NOT EXISTS campaigns(
  id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, job_id INTEGER,
  status TEXT DEFAULT 'draft', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS campaign_members(
  campaign_id INTEGER NOT NULL, candidate_id INTEGER NOT NULL,
  status TEXT DEFAULT 'queued', outreach_id INTEGER, created REAL, updated REAL,
  UNIQUE(campaign_id, candidate_id));
CREATE TABLE IF NOT EXISTS dnc(
  id INTEGER PRIMARY KEY AUTOINCREMENT, value TEXT UNIQUE, reason TEXT, created REAL);
CREATE TABLE IF NOT EXISTS resumes(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  filename TEXT NOT NULL, mime_type TEXT DEFAULT 'application/pdf',
  size INTEGER DEFAULT 0, data BLOB NOT NULL,
  storage_provider TEXT DEFAULT 'database', object_key TEXT DEFAULT '',
  bucket TEXT DEFAULT '', public_url TEXT DEFAULT '',
  checksum_sha256 TEXT DEFAULT '', etag TEXT DEFAULT '', created REAL);
CREATE TABLE IF NOT EXISTS resume_extractions(
  resume_id INTEGER PRIMARY KEY, candidate_id INTEGER NOT NULL,
  extraction TEXT DEFAULT '{}', created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS resume_capture_locks(
  candidate_id INTEGER PRIMARY KEY, touched REAL NOT NULL);
CREATE TABLE IF NOT EXISTS provider_lookups(
  id INTEGER PRIMARY KEY AUTOINCREMENT, provider TEXT NOT NULL,
  run_id TEXT NOT NULL, candidate_id INTEGER NOT NULL,
  request_key TEXT NOT NULL, status TEXT NOT NULL,
  result TEXT DEFAULT '{}', credits_spent INTEGER DEFAULT 0,
  created REAL, updated REAL,
  UNIQUE(provider, request_key));
CREATE TABLE IF NOT EXISTS lookup_runs(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT UNIQUE NOT NULL,
  metric_version TEXT NOT NULL, selected_count INTEGER NOT NULL,
  unique_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL,
  created REAL NOT NULL);
CREATE TABLE IF NOT EXISTS lookup_run_items(
  id INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT NOT NULL,
  source_identity TEXT NOT NULL, candidate_id INTEGER NOT NULL,
  phase TEXT NOT NULL, eligible INTEGER NOT NULL, outcome TEXT NOT NULL,
  found INTEGER DEFAULT 0, cached INTEGER DEFAULT 0,
  trace TEXT DEFAULT '{}', created REAL NOT NULL,
  UNIQUE(run_id, source_identity, phase));
CREATE TABLE IF NOT EXISTS api_request_activity(
  request_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
  operation TEXT DEFAULT '', status TEXT DEFAULT 'active',
  started REAL NOT NULL, finished REAL DEFAULT 0);
CREATE TABLE IF NOT EXISTS contact_lookup_queue(
  id INTEGER PRIMARY KEY AUTOINCREMENT, job_key TEXT UNIQUE NOT NULL,
  run_id TEXT NOT NULL, candidate_id INTEGER NOT NULL,
  requested_by TEXT DEFAULT '', delivery_target TEXT DEFAULT 'nexus',
  job_source TEXT DEFAULT 'extension', priority INTEGER DEFAULT 100,
  external_ref TEXT DEFAULT '',
  status TEXT DEFAULT 'queued',
  attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
  lease_until REAL DEFAULT 0, result TEXT DEFAULT '{}',
  last_error TEXT DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS contact_lookup_controls(
  requested_by TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
  updated REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nexus_candidate_links(
  identity_key TEXT PRIMARY KEY, candidate_id INTEGER NOT NULL,
  nexus_candidate_id TEXT NOT NULL UNIQUE, created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS nexus_candidate_checks(
  candidate_id INTEGER PRIMARY KEY, identity_key TEXT NOT NULL,
  blocked INTEGER NOT NULL DEFAULT 0, result TEXT DEFAULT '{}', checked REAL NOT NULL);
CREATE TABLE IF NOT EXISTS nexus_deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  resume_id INTEGER NOT NULL UNIQUE, identity_key TEXT NOT NULL, requested_by TEXT DEFAULT '',
  resume_checksum TEXT DEFAULT '', status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, next_attempt_at REAL DEFAULT 0,
  lease_until REAL DEFAULT 0, nexus_candidate_id TEXT DEFAULT '',
  operation TEXT DEFAULT '', last_error TEXT DEFAULT '',
  created REAL, updated REAL);
CREATE TABLE IF NOT EXISTS ceipal_deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, candidate_id INTEGER NOT NULL,
  user_id TEXT NOT NULL, resume_id INTEGER NOT NULL,
  status TEXT NOT NULL DEFAULT 'pending', lease_until REAL DEFAULT 0,
  last_error TEXT DEFAULT '', created REAL NOT NULL, updated REAL NOT NULL,
  UNIQUE(candidate_id,user_id));
CREATE TABLE IF NOT EXISTS watcher_email_deliveries(
  id INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT NOT NULL,
  recipient TEXT NOT NULL, status TEXT DEFAULT 'pending',
  attempts INTEGER DEFAULT 0, lease_until REAL DEFAULT 0,
  last_error TEXT DEFAULT '', created REAL, updated REAL,
  UNIQUE(event_id, recipient));
CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source, source_id);
CREATE INDEX IF NOT EXISTS idx_enrichment_events_user ON enrichment_events(auth0_sub, created);
CREATE INDEX IF NOT EXISTS idx_enrichment_events_candidate ON enrichment_events(candidate_id, created);
CREATE INDEX IF NOT EXISTS idx_candidates_provider_person ON candidates(provider_person_id);
CREATE INDEX IF NOT EXISTS idx_candidates_master ON candidates(master_candidate_id);
CREATE INDEX IF NOT EXISTS idx_pool_members_candidate ON talent_pool_members(candidate_id);
CREATE INDEX IF NOT EXISTS idx_campaign_members_candidate ON campaign_members(candidate_id);
CREATE INDEX IF NOT EXISTS idx_resumes_candidate ON resumes(candidate_id);
CREATE INDEX IF NOT EXISTS idx_resume_extractions_candidate
  ON resume_extractions(candidate_id);
CREATE INDEX IF NOT EXISTS idx_provider_lookups_run ON provider_lookups(provider, run_id);
CREATE INDEX IF NOT EXISTS idx_lookup_run_items_run ON lookup_run_items(run_id, phase);
CREATE INDEX IF NOT EXISTS idx_api_request_activity_provider
  ON api_request_activity(provider, status, started);
CREATE INDEX IF NOT EXISTS idx_contact_lookup_queue_ready
  ON contact_lookup_queue(status, next_attempt_at, created);
CREATE INDEX IF NOT EXISTS idx_contact_lookup_processing_lease
  ON contact_lookup_queue(status, lease_until, updated);
CREATE INDEX IF NOT EXISTS idx_contact_lookup_priority
  ON contact_lookup_queue(status, job_source, priority, next_attempt_at, created);
CREATE INDEX IF NOT EXISTS idx_nexus_deliveries_ready
  ON nexus_deliveries(status, next_attempt_at, lease_until);
CREATE INDEX IF NOT EXISTS idx_ceipal_deliveries_ready
  ON ceipal_deliveries(status, created);
CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_one_processing_identity
  ON nexus_deliveries(identity_key) WHERE status='processing';
CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_one_active_identity
  ON nexus_deliveries(identity_key) WHERE status IN ('processing','writing');
CREATE INDEX IF NOT EXISTS idx_watcher_email_delivery_status
  ON watcher_email_deliveries(status, lease_until);
CREATE INDEX IF NOT EXISTS idx_sms_conversations_candidate
  ON sms_conversations(candidate_id, updated);
CREATE INDEX IF NOT EXISTS idx_sms_conversations_session
  ON sms_conversations(zoom_session_id);
CREATE INDEX IF NOT EXISTS idx_sms_messages_conversation
  ON sms_messages(conversation_id, created);
"""

_POSTGRES_SCHEMA = (
    """CREATE TABLE IF NOT EXISTS users(
         id BIGSERIAL PRIMARY KEY, auth0_sub TEXT UNIQUE NOT NULL,
         email TEXT DEFAULT '', name TEXT DEFAULT '', created DOUBLE PRECISION,
         updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS enrichment_events(
         id BIGSERIAL PRIMARY KEY, auth0_sub TEXT NOT NULL,
         candidate_id BIGINT NOT NULL, status TEXT NOT NULL,
         provider TEXT DEFAULT '', run_id TEXT DEFAULT '',
         created DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS jobs(
         id BIGSERIAL PRIMARY KEY, title TEXT, location TEXT,
         description TEXT, created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS candidates(
         id BIGSERIAL PRIMARY KEY, name TEXT, location TEXT,
         hometown TEXT DEFAULT '',
         job_id BIGINT, stage TEXT DEFAULT 'new', fit_score DOUBLE PRECISION DEFAULT 0,
         phones TEXT DEFAULT '[]', emails TEXT DEFAULT '[]', addresses TEXT DEFAULT '[]',
         enrich_status TEXT DEFAULT 'pending', confidence DOUBLE PRECISION DEFAULT 0,
         verification TEXT DEFAULT '{}',
         canonical_name TEXT DEFAULT '', identity_status TEXT DEFAULT 'captured',
         identity_score DOUBLE PRECISION DEFAULT 0, identity_provider TEXT DEFAULT '',
         provider_person_id TEXT DEFAULT '', master_candidate_id BIGINT,
         identity_evidence TEXT DEFAULT '{}',
         identity_verified_at DOUBLE PRECISION DEFAULT 0,
         contact_verified_at DOUBLE PRECISION DEFAULT 0,
         contact_expires_at DOUBLE PRECISION DEFAULT 0,
         notes TEXT DEFAULT '', source TEXT DEFAULT '', source_url TEXT DEFAULT '',
         source_id TEXT DEFAULT '', created DOUBLE PRECISION, updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS medhunt_ats_routes(
         candidate_id BIGINT NOT NULL, user_id TEXT NOT NULL,
         destination TEXT NOT NULL DEFAULT 'nexus', eligibility TEXT DEFAULT '{}',
         updated DOUBLE PRECISION NOT NULL, PRIMARY KEY(candidate_id,user_id)
       )""",
    """CREATE TABLE IF NOT EXISTS outreach(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT, channel TEXT,
         subject TEXT, body TEXT, status TEXT DEFAULT 'draft', created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS talent_pools(
         id BIGSERIAL PRIMARY KEY, name TEXT UNIQUE NOT NULL,
         created DOUBLE PRECISION, updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS talent_pool_members(
         pool_id BIGINT NOT NULL, candidate_id BIGINT NOT NULL,
         created DOUBLE PRECISION, UNIQUE(pool_id, candidate_id)
       )""",
    """CREATE TABLE IF NOT EXISTS campaigns(
         id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, job_id BIGINT,
         status TEXT DEFAULT 'draft', created DOUBLE PRECISION,
         updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS campaign_members(
         campaign_id BIGINT NOT NULL, candidate_id BIGINT NOT NULL,
         status TEXT DEFAULT 'queued', outreach_id BIGINT,
         created DOUBLE PRECISION, updated DOUBLE PRECISION,
         UNIQUE(campaign_id, candidate_id)
       )""",
    """CREATE TABLE IF NOT EXISTS dnc(
         id BIGSERIAL PRIMARY KEY, value TEXT UNIQUE, reason TEXT, created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS resumes(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         filename TEXT NOT NULL, mime_type TEXT DEFAULT 'application/pdf',
         size BIGINT DEFAULT 0, data BYTEA NOT NULL, created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS resume_extractions(
         resume_id BIGINT PRIMARY KEY, candidate_id BIGINT NOT NULL,
         extraction TEXT DEFAULT '{}', created DOUBLE PRECISION,
         updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS provider_lookups(
         id BIGSERIAL PRIMARY KEY, provider TEXT NOT NULL,
         run_id TEXT NOT NULL, candidate_id BIGINT NOT NULL,
         request_key TEXT NOT NULL, status TEXT NOT NULL,
         result TEXT DEFAULT '{}', credits_spent INTEGER DEFAULT 0,
         created DOUBLE PRECISION, updated DOUBLE PRECISION,
         UNIQUE(provider, request_key)
       )""",
    """CREATE TABLE IF NOT EXISTS lookup_runs(
         id BIGSERIAL PRIMARY KEY, run_id TEXT UNIQUE NOT NULL,
         metric_version TEXT NOT NULL, selected_count INTEGER NOT NULL,
         unique_count INTEGER NOT NULL, eligible_count INTEGER NOT NULL,
         created DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS lookup_run_items(
         id BIGSERIAL PRIMARY KEY, run_id TEXT NOT NULL,
         source_identity TEXT NOT NULL, candidate_id BIGINT NOT NULL,
         phase TEXT NOT NULL, eligible INTEGER NOT NULL, outcome TEXT NOT NULL,
         found INTEGER DEFAULT 0, cached INTEGER DEFAULT 0,
         trace TEXT DEFAULT '{}', created DOUBLE PRECISION NOT NULL,
         UNIQUE(run_id, source_identity, phase)
       )""",
    """CREATE TABLE IF NOT EXISTS nexus_candidate_links(
         identity_key TEXT PRIMARY KEY, candidate_id BIGINT NOT NULL,
         nexus_candidate_id TEXT NOT NULL UNIQUE,
         created DOUBLE PRECISION, updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS resume_capture_locks(
         candidate_id BIGINT PRIMARY KEY, touched DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS nexus_deliveries(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         resume_id BIGINT NOT NULL UNIQUE, identity_key TEXT NOT NULL, requested_by TEXT DEFAULT '',
         resume_checksum TEXT DEFAULT '', status TEXT DEFAULT 'pending',
         attempts INTEGER DEFAULT 0, next_attempt_at DOUBLE PRECISION DEFAULT 0,
         lease_until DOUBLE PRECISION DEFAULT 0,
         nexus_candidate_id TEXT DEFAULT '', operation TEXT DEFAULT '',
         last_error TEXT DEFAULT '', created DOUBLE PRECISION,
          updated DOUBLE PRECISION
        )""",
    """CREATE TABLE IF NOT EXISTS ceipal_deliveries(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         user_id TEXT NOT NULL, resume_id BIGINT NOT NULL,
         status TEXT NOT NULL DEFAULT 'pending', lease_until DOUBLE PRECISION DEFAULT 0,
         last_error TEXT DEFAULT '', created DOUBLE PRECISION NOT NULL,
         updated DOUBLE PRECISION NOT NULL, UNIQUE(candidate_id,user_id)
       )""",
    """CREATE TABLE IF NOT EXISTS watcher_email_deliveries(
         id BIGSERIAL PRIMARY KEY, event_id TEXT NOT NULL,
         recipient TEXT NOT NULL, status TEXT DEFAULT 'pending',
         attempts INTEGER DEFAULT 0, lease_until DOUBLE PRECISION DEFAULT 0,
         last_error TEXT DEFAULT '', created DOUBLE PRECISION,
         updated DOUBLE PRECISION, UNIQUE(event_id, recipient)
       )""",
    """CREATE TABLE IF NOT EXISTS sms_consents(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         phone_key TEXT NOT NULL, phone TEXT NOT NULL, status TEXT NOT NULL,
         source TEXT NOT NULL, evidence TEXT DEFAULT '',
         disclosure_version TEXT DEFAULT 'v1', captured_by TEXT DEFAULT '',
         captured_at DOUBLE PRECISION, created DOUBLE PRECISION,
         updated DOUBLE PRECISION, UNIQUE(candidate_id, phone_key)
       )""",
    """CREATE TABLE IF NOT EXISTS sms_conversations(
         id BIGSERIAL PRIMARY KEY, candidate_id BIGINT NOT NULL,
         nexus_candidate_id TEXT DEFAULT '', candidate_name TEXT DEFAULT '',
         candidate_phone TEXT NOT NULL, phone_key TEXT NOT NULL,
         initiated_by TEXT DEFAULT '', assigned_recruiter_id TEXT DEFAULT '',
         assigned_recruiter_email TEXT DEFAULT '', assigned_recruiter_name TEXT DEFAULT '',
         zoom_sender_number TEXT DEFAULT '', zoom_sender_user_id TEXT DEFAULT '',
         zoom_session_id TEXT DEFAULT '',
         status TEXT DEFAULT 'open', created DOUBLE PRECISION,
         updated DOUBLE PRECISION, last_message_at DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS sms_messages(
         id BIGSERIAL PRIMARY KEY, conversation_id BIGINT NOT NULL,
         direction TEXT NOT NULL, body TEXT NOT NULL, status TEXT DEFAULT 'queued',
         zoom_message_id TEXT DEFAULT '', request_id TEXT UNIQUE,
         sender_user_id TEXT DEFAULT '', sender_name TEXT DEFAULT '',
         sender_number TEXT DEFAULT '', zoom_user_id TEXT DEFAULT '',
         failure_reason TEXT DEFAULT '', created DOUBLE PRECISION,
         updated DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS sms_webhook_events(
         event_key TEXT PRIMARY KEY, event_type TEXT NOT NULL,
         created DOUBLE PRECISION
       )""",
    """CREATE TABLE IF NOT EXISTS api_request_activity(
         request_id TEXT PRIMARY KEY, provider TEXT NOT NULL,
         operation TEXT DEFAULT '', status TEXT DEFAULT 'active',
         started DOUBLE PRECISION NOT NULL,
         finished DOUBLE PRECISION DEFAULT 0
       )""",
    """CREATE TABLE IF NOT EXISTS contact_lookup_queue(
         id BIGSERIAL PRIMARY KEY, job_key TEXT UNIQUE NOT NULL,
         run_id TEXT NOT NULL, candidate_id BIGINT NOT NULL,
         requested_by TEXT DEFAULT '', delivery_target TEXT DEFAULT 'nexus',
         job_source TEXT DEFAULT 'extension', priority INTEGER DEFAULT 100,
         external_ref TEXT DEFAULT '',
         status TEXT DEFAULT 'queued',
         attempts INTEGER DEFAULT 0,
         next_attempt_at DOUBLE PRECISION DEFAULT 0,
         lease_until DOUBLE PRECISION DEFAULT 0, result TEXT DEFAULT '{}',
         last_error TEXT DEFAULT '', created DOUBLE PRECISION NOT NULL,
         updated DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS contact_lookup_controls(
         requested_by TEXT PRIMARY KEY, paused INTEGER NOT NULL DEFAULT 0,
         updated DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS nexus_candidate_checks(
         candidate_id BIGINT PRIMARY KEY, identity_key TEXT NOT NULL,
         blocked INTEGER NOT NULL DEFAULT 0, result TEXT DEFAULT '{}',
         checked DOUBLE PRECISION NOT NULL
       )""",
    """CREATE TABLE IF NOT EXISTS sms_outreach_claims(
         candidate_id BIGINT PRIMARY KEY, phone_key TEXT NOT NULL,
         status TEXT DEFAULT 'sending', created DOUBLE PRECISION,
         updated DOUBLE PRECISION
       )""",
    # Older deployments may already contain one or more of these tables from
    # before candidate-level attribution was added. CREATE TABLE IF NOT EXISTS
    # does not evolve an existing table, so repair every candidate-bearing
    # table before creating indexes or serving requests. Keep the added columns
    # nullable so historical rows are preserved; all current writes supply the
    # candidate id.
    "ALTER TABLE enrichment_events ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE sms_conversations ADD COLUMN IF NOT EXISTS zoom_sender_user_id TEXT DEFAULT ''",
    "ALTER TABLE sms_messages ADD COLUMN IF NOT EXISTS sender_user_id TEXT DEFAULT ''",
    "ALTER TABLE sms_messages ADD COLUMN IF NOT EXISTS sender_name TEXT DEFAULT ''",
    "ALTER TABLE sms_messages ADD COLUMN IF NOT EXISTS sender_number TEXT DEFAULT ''",
    "ALTER TABLE sms_messages ADD COLUMN IF NOT EXISTS zoom_user_id TEXT DEFAULT ''",
    "ALTER TABLE outreach ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE talent_pool_members ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE campaign_members ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE resume_extractions ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE provider_lookups ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE lookup_run_items ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE nexus_candidate_links ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE resume_capture_locks ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE nexus_deliveries ADD COLUMN IF NOT EXISTS candidate_id BIGINT",
    "ALTER TABLE nexus_deliveries ADD COLUMN IF NOT EXISTS requested_by TEXT DEFAULT ''",
    "ALTER TABLE contact_lookup_queue ADD COLUMN IF NOT EXISTS delivery_target TEXT DEFAULT 'nexus'",
    "ALTER TABLE contact_lookup_queue ADD COLUMN IF NOT EXISTS job_source TEXT DEFAULT 'extension'",
    "ALTER TABLE contact_lookup_queue ADD COLUMN IF NOT EXISTS priority INTEGER DEFAULT 100",
    "ALTER TABLE contact_lookup_queue ADD COLUMN IF NOT EXISTS external_ref TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS hometown TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_url TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS source_id TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS verification TEXT DEFAULT '{}'",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS canonical_name TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS identity_status TEXT DEFAULT 'captured'",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS identity_score DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS identity_provider TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS provider_person_id TEXT DEFAULT ''",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS master_candidate_id BIGINT",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS identity_evidence TEXT DEFAULT '{}'",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS identity_verified_at DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS contact_verified_at DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE candidates ADD COLUMN IF NOT EXISTS contact_expires_at DOUBLE PRECISION DEFAULT 0",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS storage_provider TEXT DEFAULT 'database'",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS object_key TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS bucket TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS public_url TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS checksum_sha256 TEXT DEFAULT ''",
    "ALTER TABLE resumes ADD COLUMN IF NOT EXISTS etag TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_candidates_source ON candidates(source, source_id)",
    "CREATE INDEX IF NOT EXISTS idx_enrichment_events_user ON enrichment_events(auth0_sub, created)",
    "CREATE INDEX IF NOT EXISTS idx_enrichment_events_candidate ON enrichment_events(candidate_id, created)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_provider_person ON candidates(provider_person_id)",
    "CREATE INDEX IF NOT EXISTS idx_candidates_master ON candidates(master_candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_pool_members_candidate ON talent_pool_members(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_campaign_members_candidate ON campaign_members(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_resumes_candidate ON resumes(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_resume_extractions_candidate "
    "ON resume_extractions(candidate_id)",
    "CREATE INDEX IF NOT EXISTS idx_provider_lookups_run ON provider_lookups(provider, run_id)",
    "CREATE INDEX IF NOT EXISTS idx_lookup_run_items_run ON lookup_run_items(run_id, phase)",
    "CREATE INDEX IF NOT EXISTS idx_api_request_activity_provider "
    "ON api_request_activity(provider, status, started)",
    "CREATE INDEX IF NOT EXISTS idx_contact_lookup_queue_ready "
    "ON contact_lookup_queue(status, next_attempt_at, created)",
    "DROP INDEX IF EXISTS idx_contact_lookup_one_processing",
    "CREATE INDEX IF NOT EXISTS idx_contact_lookup_processing_lease "
    "ON contact_lookup_queue(status, lease_until, updated)",
    "CREATE INDEX IF NOT EXISTS idx_contact_lookup_priority "
    "ON contact_lookup_queue(status, job_source, priority, next_attempt_at, created)",
    "CREATE INDEX IF NOT EXISTS idx_nexus_deliveries_ready "
    "ON nexus_deliveries(status, next_attempt_at, lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_ceipal_deliveries_ready "
    "ON ceipal_deliveries(status, created)",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_one_processing_identity "
    "ON nexus_deliveries(identity_key) WHERE status='processing'",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_nexus_one_active_identity "
    "ON nexus_deliveries(identity_key) WHERE status IN ('processing','writing')",
    "CREATE INDEX IF NOT EXISTS idx_watcher_email_delivery_status "
    "ON watcher_email_deliveries(status, lease_until)",
    "CREATE INDEX IF NOT EXISTS idx_sms_conversations_candidate "
    "ON sms_conversations(candidate_id, updated)",
    "CREATE INDEX IF NOT EXISTS idx_sms_conversations_session "
    "ON sms_conversations(zoom_session_id)",
    "CREATE INDEX IF NOT EXISTS idx_sms_messages_conversation "
    "ON sms_messages(conversation_id, created)",
)

_POSTGRES_SCHEMA_READY = False
_SCHEMA_LOCK = threading.Lock()
_CONTACT_LOOKUP_ENQUEUE_LOCK = threading.Lock()
_CONTACT_LOOKUP_CLAIM_LOCK = threading.Lock()
_POSTGRES_POOL = None
_POSTGRES_POOL_LOCK = threading.Lock()
_CANDIDATE_FIELDS = {
    "name", "location", "hometown", "job_id", "stage", "fit_score", "phones", "emails",
    "addresses", "enrich_status", "confidence", "notes", "source",
    "source_url", "source_id", "verification", "canonical_name",
    "identity_status", "identity_score", "identity_provider",
    "provider_person_id", "identity_evidence", "identity_verified_at",
    "contact_verified_at", "contact_expires_at", "master_candidate_id",
}

# A complete installation can be recognized with one inexpensive query.  This
# avoids replaying every CREATE/ALTER statement over a remote Neon connection
# each time the local desktop backend starts.  The full idempotent schema below
# still runs whenever a table, migration column, or required index is missing.
_POSTGRES_REQUIRED_TABLES = (
    "users", "enrichment_events", "jobs", "candidates", "outreach", "talent_pools", "talent_pool_members",
    "campaigns", "campaign_members", "dnc", "resumes", "resume_extractions",
    "provider_lookups", "lookup_runs", "lookup_run_items", "api_request_activity",
    "contact_lookup_queue", "contact_lookup_controls",
    "nexus_candidate_links", "nexus_candidate_checks", "resume_capture_locks", "nexus_deliveries", "ceipal_deliveries",
    "watcher_email_deliveries",
    "sms_consents", "sms_conversations", "sms_messages", "sms_webhook_events",
    "sms_outreach_claims",
)
_POSTGRES_REQUIRED_COLUMNS = {
    "enrichment_events": ("candidate_id",),
    "outreach": ("candidate_id",),
    "talent_pool_members": ("candidate_id",),
    "campaign_members": ("candidate_id",),
    "candidates": (
        "hometown", "source", "source_url", "source_id", "verification",
        "canonical_name", "identity_status", "identity_score",
        "identity_provider", "provider_person_id", "master_candidate_id",
        "identity_evidence", "identity_verified_at", "contact_verified_at",
        "contact_expires_at",
    ),
    "resumes": (
        "candidate_id", "storage_provider", "object_key", "bucket", "public_url",
        "checksum_sha256", "etag",
    ),
    "resume_extractions": ("candidate_id",),
    "provider_lookups": ("candidate_id",),
    "lookup_run_items": ("candidate_id",),
    "nexus_candidate_links": ("candidate_id",),
    "contact_lookup_queue": ("delivery_target", "job_source", "priority", "external_ref"),
    "resume_capture_locks": ("candidate_id",),
    "nexus_deliveries": ("candidate_id",),
    "sms_conversations": ("zoom_sender_user_id",),
    "sms_messages": ("sender_user_id", "sender_name", "sender_number", "zoom_user_id"),
}
_POSTGRES_REQUIRED_INDEXES = (
    "idx_enrichment_events_user", "idx_enrichment_events_candidate",
    "idx_candidates_source", "idx_candidates_provider_person",
    "idx_candidates_master", "idx_pool_members_candidate",
    "idx_campaign_members_candidate", "idx_resumes_candidate",
    "idx_resume_extractions_candidate", "idx_provider_lookups_run",
    "idx_lookup_run_items_run", "idx_api_request_activity_provider",
    "idx_contact_lookup_queue_ready", "idx_contact_lookup_processing_lease",
    "idx_contact_lookup_priority",
    "idx_nexus_deliveries_ready",
    "idx_ceipal_deliveries_ready",
    "idx_nexus_one_processing_identity", "idx_nexus_one_active_identity",
    "idx_watcher_email_delivery_status",
    "idx_sms_conversations_candidate", "idx_sms_conversations_session",
    "idx_sms_messages_conversation",
)


class _Connection:
    def __init__(self, raw, postgres=False):
        self.raw = raw
        self.postgres = postgres

    def execute(self, query, args=()):
        if self.postgres:
            query = query.replace("?", "%s")
        return self.raw.execute(query, args)

    @contextmanager
    def transaction(self):
        """Group several statements atomically on an autocommit connection."""
        if not self.postgres:
            # ``_conn()`` already wraps each SQLite block in one commit/rollback.
            yield self
            return
        with self.raw.transaction():
            yield self


def backend_name():
    return "postgresql" if config.DATABASE_URL else "sqlite"


def _prepare_postgres(connection):
    global _POSTGRES_SCHEMA_READY
    if _POSTGRES_SCHEMA_READY:
        return
    with _SCHEMA_LOCK:
        if _POSTGRES_SCHEMA_READY:
            return
        if _postgres_schema_is_current(connection):
            _POSTGRES_SCHEMA_READY = True
            return
        for statement in _POSTGRES_SCHEMA:
            connection.execute(statement)
        if not connection.raw.autocommit:
            connection.raw.commit()
        _POSTGRES_SCHEMA_READY = True


def _configure_postgres_namespace(connection):
    """Create the isolated Medhunt namespace once per physical connection."""
    schema = config.DATABASE_SCHEMA
    # DATABASE_SCHEMA is validated as a PostgreSQL identifier in config.py.
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    connection.execute(f"CREATE SCHEMA IF NOT EXISTS {quoted_schema}")


def _activate_postgres_namespace(connection):
    """Pin every unit of work to Medhunt even through a transaction pooler."""
    schema = config.DATABASE_SCHEMA
    quoted_schema = '"' + schema.replace('"', '""') + '"'
    connection.execute(f"SET LOCAL search_path TO {quoted_schema}")


def _postgres_schema_is_current(connection):
    """Return true when the remote schema already contains every dependency."""
    schema = config.DATABASE_SCHEMA
    relations = _POSTGRES_REQUIRED_TABLES + _POSTGRES_REQUIRED_INDEXES
    relation_checks = [
        f"to_regclass('{schema}.{name}') IS NOT NULL" for name in relations
    ]
    column_checks = [
        "EXISTS (SELECT 1 FROM information_schema.columns "
        f"WHERE table_schema = '{schema}' "
        f"AND table_name = '{table}' AND column_name = '{column}')"
        for table, columns in _POSTGRES_REQUIRED_COLUMNS.items()
        for column in columns
    ]
    query = "SELECT " + " AND ".join(relation_checks + column_checks) + " AS ready"
    row = connection.execute(query).fetchone()
    if row is None:
        return False
    if isinstance(row, dict):
        return bool(row.get("ready"))
    return bool(row[0])


@contextmanager
def _conn():
    if config.DATABASE_URL:
        global _POSTGRES_POOL
        try:
            import psycopg
            from psycopg.rows import dict_row
        except ImportError as exc:  # pragma: no cover - dependency error is explicit
            raise RuntimeError(
                "DATABASE_URL is configured but psycopg is not installed. "
                "Run: pip install -r requirements.txt"
            ) from exc
        try:
            from psycopg_pool import ConnectionPool
        except ImportError:
            # Keep the service available if a deployment installs psycopg but
            # accidentally omits the optional pool package. This path is
            # slower, but requests and the queue continue to function.
            raw = psycopg.connect(
                config.DATABASE_URL,
                row_factory=dict_row,
                autocommit=True,
                connect_timeout=config.DATABASE_CONNECT_TIMEOUT,
                prepare_threshold=None,
            )
            connection = _Connection(raw, postgres=True)
            try:
                _configure_postgres_namespace(connection)
                with raw.transaction():
                    _activate_postgres_namespace(connection)
                    _prepare_postgres(connection)
                    yield connection
            finally:
                raw.close()
            return

        if _POSTGRES_POOL is None:
            with _POSTGRES_POOL_LOCK:
                if _POSTGRES_POOL is None:
                    def configure(raw):
                        _configure_postgres_namespace(_Connection(raw, postgres=True))

                    _POSTGRES_POOL = ConnectionPool(
                        conninfo=config.DATABASE_URL,
                        min_size=config.DATABASE_POOL_SIZE,
                        max_size=config.DATABASE_POOL_SIZE,
                        timeout=max(60, config.DATABASE_CONNECT_TIMEOUT),
                        configure=configure,
                        kwargs={
                            "row_factory": dict_row,
                            "autocommit": True,
                            "connect_timeout": config.DATABASE_CONNECT_TIMEOUT,
                            # A Neon transaction pooler can move work between
                            # physical connections. Avoid server-side prepared
                            # statements whose cached types may cross schemas.
                            "prepare_threshold": None,
                        },
                    )

        # A bounded pool lets queue workers perform short Neon transactions in
        # parallel while keeping the database connection count predictable.
        with _POSTGRES_POOL.connection(
            timeout=max(60, config.DATABASE_CONNECT_TIMEOUT),
        ) as raw:
            connection = _Connection(raw, postgres=True)
            try:
                # Hosted PostgreSQL poolers may discard session-level SET
                # values between autocommit statements. Keep schema setup and
                # every application query in one transaction, with a SET LOCAL
                # that is guaranteed to apply to that transaction's server
                # connection.
                with raw.transaction():
                    _activate_postgres_namespace(connection)
                    _prepare_postgres(connection)
                    yield connection
            except Exception:
                try:
                    if not raw.autocommit:
                        raw.rollback()
                except Exception:
                    pass
                raise
        return

    sqlite_path = Path(config.DB_PATH)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    raw = sqlite3.connect(str(sqlite_path))
    connection = _Connection(raw)
    try:
        raw.row_factory = sqlite3.Row
        raw.executescript(_SQLITE_SCHEMA)
        raw.execute("DROP INDEX IF EXISTS idx_contact_lookup_one_processing")
        columns = {row["name"] for row in raw.execute("PRAGMA table_info(candidates)")}
        for name, definition in (
            ("hometown", "TEXT DEFAULT ''"),
            ("source", "TEXT DEFAULT ''"),
            ("source_url", "TEXT DEFAULT ''"),
            ("source_id", "TEXT DEFAULT ''"),
            ("verification", "TEXT DEFAULT '{}'"),
            ("canonical_name", "TEXT DEFAULT ''"),
            ("identity_status", "TEXT DEFAULT 'captured'"),
            ("identity_score", "REAL DEFAULT 0"),
            ("identity_provider", "TEXT DEFAULT ''"),
            ("provider_person_id", "TEXT DEFAULT ''"),
            ("master_candidate_id", "INTEGER"),
            ("identity_evidence", "TEXT DEFAULT '{}'"),
            ("identity_verified_at", "REAL DEFAULT 0"),
            ("contact_verified_at", "REAL DEFAULT 0"),
            ("contact_expires_at", "REAL DEFAULT 0"),
        ):
            if name not in columns:
                raw.execute(f"ALTER TABLE candidates ADD COLUMN {name} {definition}")
        resume_columns = {row["name"] for row in raw.execute("PRAGMA table_info(resumes)")}
        for name, definition in (
            ("storage_provider", "TEXT DEFAULT 'database'"),
            ("object_key", "TEXT DEFAULT ''"),
            ("bucket", "TEXT DEFAULT ''"),
            ("public_url", "TEXT DEFAULT ''"),
            ("checksum_sha256", "TEXT DEFAULT ''"),
            ("etag", "TEXT DEFAULT ''"),
        ):
            if name not in resume_columns:
                raw.execute(f"ALTER TABLE resumes ADD COLUMN {name} {definition}")
        conversation_columns = {
            row["name"] for row in raw.execute("PRAGMA table_info(sms_conversations)")
        }
        if "zoom_sender_user_id" not in conversation_columns:
            raw.execute(
                "ALTER TABLE sms_conversations ADD COLUMN zoom_sender_user_id TEXT DEFAULT ''"
            )
        message_columns = {
            row["name"] for row in raw.execute("PRAGMA table_info(sms_messages)")
        }
        for name in ("sender_user_id", "sender_name", "sender_number", "zoom_user_id"):
            if name not in message_columns:
                raw.execute(f"ALTER TABLE sms_messages ADD COLUMN {name} TEXT DEFAULT ''")
        queue_columns = {
            row["name"] for row in raw.execute("PRAGMA table_info(contact_lookup_queue)")
        }
        if "delivery_target" not in queue_columns:
            raw.execute(
                "ALTER TABLE contact_lookup_queue ADD COLUMN delivery_target TEXT DEFAULT 'nexus'"
            )
        for name, definition in (
            ("job_source", "TEXT DEFAULT 'extension'"),
            ("priority", "INTEGER DEFAULT 100"),
            ("external_ref", "TEXT DEFAULT ''"),
        ):
            if name not in queue_columns:
                raw.execute(f"ALTER TABLE contact_lookup_queue ADD COLUMN {name} {definition}")
        nexus_delivery_columns = {
            row["name"] for row in raw.execute("PRAGMA table_info(nexus_deliveries)")
        }
        if "requested_by" not in nexus_delivery_columns:
            raw.execute("ALTER TABLE nexus_deliveries ADD COLUMN requested_by TEXT DEFAULT ''")
        yield connection
        raw.commit()
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()


def _row(row):
    data = dict(row)
    for key in ("phones", "emails", "addresses", "verification", "identity_evidence"):
        if key in data and isinstance(data[key], str):
            try:
                data[key] = json.loads(data[key])
            except Exception:
                data[key] = {} if key in ("verification", "identity_evidence") else []
    return data


def initialize():
    """Create/migrate schema and warm the configured database connection."""
    with _conn() as connection:
        connection.execute("SELECT 1")
        _migrate_all_nexus_identity_keys(connection)


def _scalar(cursor):
    row = cursor.fetchone()
    if row is None:
        return None
    if isinstance(row, dict):
        return next(iter(row.values()))
    return row[0]


def _insert_id(connection, query, args):
    if connection.postgres:
        row = connection.execute(f"{query} RETURNING id", args).fetchone()
        return row["id"]
    return connection.execute(query, args).lastrowid


# ---- jobs ----
def create_job(title, location="", description=""):
    with _conn() as connection:
        return _insert_id(
            connection,
            "INSERT INTO jobs(title,location,description,created) VALUES(?,?,?,?)",
            (title, location, description, time.time()),
        )


def list_jobs():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM jobs ORDER BY created DESC"
        )]


def get_job(job_id):
    with _conn() as connection:
        row = connection.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        return dict(row) if row else None


# ---- candidates ----
def add_candidate(
    name,
    location="",
    job_id=None,
    notes="",
    source="",
    source_url="",
    source_id="",
    hometown="",
):
    now = time.time()
    with _conn() as connection:
        return _insert_id(
            connection,
            """INSERT INTO candidates(
                 name,location,hometown,job_id,notes,source,source_url,source_id,created,updated
               ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                name.strip(), location.strip(), hometown.strip(), job_id, notes.strip(),
                source.strip(), source_url.strip(), source_id.strip(), now, now,
            ),
        )


def add_candidates_bulk(rows, job_id=None):
    """rows: list of candidate dictionaries. Returns inserted IDs."""
    ids = []
    for row in rows:
        name = (row.get("name") or "").strip()
        if name:
            ids.append(add_candidate(
                name,
                row.get("location", ""),
                job_id,
                notes=row.get("notes", ""),
                source=row.get("source", ""),
                source_url=row.get("source_url", ""),
                source_id=row.get("source_id", ""),
                hometown=row.get("hometown", ""),
            ))
    return ids


def list_candidates(job_id=None, stage=None):
    query = "SELECT * FROM candidates"
    conditions, args = [], []
    if job_id is not None:
        conditions.append("job_id=?")
        args.append(job_id)
    if stage:
        conditions.append("stage=?")
        args.append(stage)
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY fit_score DESC, created DESC"
    with _conn() as connection:
        return [_row(row) for row in connection.execute(query, args)]


def get_candidate(candidate_id):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        return _row(row) if row else None


def get_candidates(candidate_ids):
    """Load a bounded candidate selection in one database round trip."""
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    if not ordered_ids:
        return []
    placeholders = ",".join("?" for _ in ordered_ids)
    with _conn() as connection:
        rows = connection.execute(
            f"SELECT * FROM candidates WHERE id IN ({placeholders})",
            ordered_ids,
        ).fetchall()
    by_id = {int(row["id"]): _row(row) for row in rows}
    return [by_id[candidate_id] for candidate_id in ordered_ids if candidate_id in by_id]


def get_candidate_ats_route(candidate_id: int, user_id: str) -> dict | None:
    owner = str(user_id or "").strip()[:200]
    if not owner:
        return None
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM medhunt_ats_routes WHERE candidate_id=? AND user_id=?",
            (int(candidate_id), owner),
        ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["eligibility"] = json.loads(result.get("eligibility") or "{}")
    except (TypeError, ValueError):
        result["eligibility"] = {}
    return result


def set_candidate_ats_route(
    candidate_id: int, user_id: str, destination: str, eligibility=None,
) -> dict:
    owner = str(user_id or "").strip()[:200]
    if not owner:
        raise ValueError("A user id is required for ATS routing.")
    target = "ceipal" if str(destination or "").casefold() == "ceipal" else "nexus"
    existing = get_candidate_ats_route(candidate_id, owner)
    saved_eligibility = (
        dict(eligibility or {}) if eligibility is not None
        else (existing.get("eligibility") or {} if existing and existing.get("destination") == target else {})
    )
    now = time.time()
    with _conn() as connection:
        connection.execute(
            """INSERT INTO medhunt_ats_routes(candidate_id,user_id,destination,eligibility,updated)
               VALUES(?,?,?,?,?) ON CONFLICT(candidate_id,user_id) DO UPDATE SET
               destination=excluded.destination,eligibility=excluded.eligibility,
               updated=excluded.updated""",
            (int(candidate_id), owner, target,
             json.dumps(saved_eligibility, separators=(",", ":")), now),
        )
    return {
        "candidate_id": int(candidate_id), "user_id": owner,
        "destination": target, "eligibility": saved_eligibility, "updated": now,
    }


def enqueue_ceipal_delivery(candidate_id: int, resume_id: int, user_id: str) -> dict:
    """Persist one CEIPAL job per candidate and recruiter after resume storage."""
    now = time.time()
    with _conn() as connection:
        connection.execute(
            """INSERT INTO ceipal_deliveries(
                 candidate_id,user_id,resume_id,status,created,updated
               ) VALUES(?,?,?,'pending',?,?)
               ON CONFLICT(candidate_id,user_id) DO UPDATE SET
                 resume_id=excluded.resume_id,
                 status=CASE WHEN ceipal_deliveries.status IN
                   ('uploaded','already_in_ceipal','writing','indeterminate')
                   THEN ceipal_deliveries.status ELSE 'pending' END,
                 updated=excluded.updated""",
            (int(candidate_id), str(user_id or "local")[:200], int(resume_id), now, now),
        )
        row = connection.execute(
            "SELECT * FROM ceipal_deliveries WHERE candidate_id=? AND user_id=?",
            (int(candidate_id), str(user_id or "local")[:200]),
        ).fetchone()
    return dict(row)


def claim_ceipal_delivery(lease_seconds: float = 300) -> dict | None:
    """Claim safely across workers; an expired in-flight call needs review."""
    now = time.time()
    lease_until = now + max(30.0, float(lease_seconds))
    with _conn() as connection, connection.transaction():
        connection.execute(
            """UPDATE ceipal_deliveries
               SET status='indeterminate',lease_until=0,
                   last_error='CEIPAL request outcome unknown after worker interruption',updated=?
               WHERE status IN ('processing','writing') AND lease_until<=?""",
            (now, now),
        )
        query = (
            "SELECT * FROM ceipal_deliveries WHERE status='pending' "
            "ORDER BY created,id LIMIT 1"
        )
        if connection.postgres:
            query += " FOR UPDATE SKIP LOCKED"
        row = connection.execute(query).fetchone()
        if not row:
            return None
        updated = connection.execute(
            """UPDATE ceipal_deliveries SET status='processing',lease_until=?,updated=?
               WHERE id=? AND status='pending'""",
            (lease_until, now, int(row["id"])),
        )
        if updated.rowcount != 1:
            return None
        return {**dict(row), "status": "processing", "lease_until": lease_until}


def mark_ceipal_delivery_writing(delivery_id: int, lease_until: float) -> bool:
    with _conn() as connection:
        updated = connection.execute(
            """UPDATE ceipal_deliveries SET status='writing',updated=?
               WHERE id=? AND status='processing' AND lease_until=?""",
            (time.time(), int(delivery_id), float(lease_until)),
        )
    return updated.rowcount == 1


def finish_ceipal_delivery(delivery_id: int, lease_until: float,
                           status: str, error: str = "") -> bool:
    allowed = {"uploaded", "already_in_ceipal", "failed", "indeterminate"}
    if status not in allowed:
        raise ValueError("Unsupported CEIPAL delivery status")
    with _conn() as connection:
        updated = connection.execute(
            """UPDATE ceipal_deliveries SET status=?,lease_until=0,last_error=?,updated=?
               WHERE id=? AND status='writing' AND lease_until=?""",
            (status, str(error or "")[:240], time.time(), int(delivery_id), float(lease_until)),
        )
    return updated.rowcount == 1


def get_ceipal_delivery(candidate_id: int, user_id: str) -> dict | None:
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM ceipal_deliveries WHERE candidate_id=? AND user_id=?",
            (int(candidate_id), str(user_id or "local")[:200]),
        ).fetchone()
    return dict(row) if row else None


def get_nexus_candidate_check(candidate_id: int):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM nexus_candidate_checks WHERE candidate_id=?",
            (int(candidate_id),),
        ).fetchone()
    if not row:
        return None
    data = dict(row)
    try:
        data["result"] = json.loads(data.get("result") or "{}")
    except (TypeError, ValueError):
        data["result"] = {}
    data["blocked"] = bool(data.get("blocked"))
    return data


def save_nexus_candidate_check(
    candidate_id: int, identity_key: str, result: dict, checked: float | None = None,
):
    checked_at = float(checked or time.time())
    blocked = int(bool(result.get("blocked")))
    payload = json.dumps(result, separators=(",", ":"))
    with _conn() as connection:
        connection.execute(
            """INSERT INTO nexus_candidate_checks(
                 candidate_id,identity_key,blocked,result,checked
               ) VALUES(?,?,?,?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                 identity_key=excluded.identity_key, blocked=excluded.blocked,
                 result=excluded.result, checked=excluded.checked""",
            (int(candidate_id), identity_key, blocked, payload, checked_at),
        )
    return {"candidate_id": int(candidate_id), "identity_key": identity_key,
            "blocked": bool(blocked), "result": result, "checked": checked_at}


def get_candidate_by_source(source, source_id):
    if not source or not source_id:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM candidates
               WHERE source=? AND source_id=?
               ORDER BY created DESC LIMIT 1""",
            (source.strip(), source_id.strip()),
        ).fetchone()
        return _row(row) if row else None


def get_candidate_by_identity(source, name, location=""):
    if not source or not name:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM candidates
               WHERE LOWER(source)=LOWER(?) AND LOWER(name)=LOWER(?)
                 AND LOWER(COALESCE(location,''))=LOWER(?)
               ORDER BY created DESC LIMIT 1""",
            (source.strip(), name.strip(), location.strip()),
        ).fetchone()
        return _row(row) if row else None


def find_identity_candidates(name: str, limit: int = 25) -> list[dict]:
    """Return previously resolved records that might represent ``name``.

    This is intentionally a broad, read-only prefilter. The identity resolver
    applies deterministic name/location/employment rules before reusing any
    record, so this query never merges candidates by itself.
    """
    tokens = [part for part in str(name or "").casefold().split() if part]
    if not tokens:
        return []
    first = tokens[0]
    with _conn() as connection:
        rows = connection.execute(
            """SELECT * FROM candidates
               WHERE identity_status IN ('verified','recruiter_confirmed')
                 AND LOWER(COALESCE(NULLIF(canonical_name,''),name)) LIKE ?
               ORDER BY identity_score DESC, identity_verified_at DESC
               LIMIT ?""",
            (f"{first}%", max(1, min(100, int(limit)))),
        ).fetchall()
    return [_row(row) for row in rows]


def get_candidate_by_provider_person_id(provider_person_id: str, exclude_id: int | None = None):
    value = str(provider_person_id or "").strip()
    if not value:
        return None
    query = """SELECT * FROM candidates
               WHERE provider_person_id=?
                 AND identity_status IN ('verified','recruiter_confirmed')"""
    args = [value]
    if exclude_id is not None:
        query += " AND id<>?"
        args.append(int(exclude_id))
    query += " ORDER BY COALESCE(master_candidate_id,id), identity_verified_at DESC LIMIT 1"
    with _conn() as connection:
        row = connection.execute(query, args).fetchone()
    return _row(row) if row else None


# ---- provider lookup cache / credit accounting ----
def get_provider_lookup(provider, request_key):
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM provider_lookups
               WHERE provider=? AND request_key=? LIMIT 1""",
            (provider.strip().lower(), request_key.strip()),
        ).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["result"] = json.loads(data.get("result") or "{}")
        except Exception:
            data["result"] = {}
        return data


def recent_provider_lookups_for_candidate(provider, candidate_id, limit=10):
    """Return recent immutable provider observations for one local candidate.

    Request-policy versions are deliberately part of provider request keys, so
    a safer policy release normally misses older cache rows.  Successful
    person evidence can still be useful after a policy bump, provided the
    caller revalidates it against the candidate's *current* source identity.
    This helper does not decide trust and never returns another candidate's
    row.
    """
    with _conn() as connection:
        rows = connection.execute(
            """SELECT * FROM provider_lookups
               WHERE provider=? AND candidate_id=?
               ORDER BY updated DESC, id DESC LIMIT ?""",
            (
                provider.strip().lower(), int(candidate_id),
                max(1, min(50, int(limit or 10))),
            ),
        ).fetchall()
    output = []
    for row in rows:
        data = dict(row)
        try:
            data["result"] = json.loads(data.get("result") or "{}")
        except Exception:
            data["result"] = {}
        output.append(data)
    return output


def begin_lookup_run(run_id: str, cohort: dict):
    """Freeze one coverage denominator as append-only input observations."""
    now = time.time()
    normalized_run_id = str(run_id or "").strip()
    if not normalized_run_id:
        raise ValueError("run_id is required")
    with _conn() as connection:
        with connection.transaction():
            connection.execute(
                """INSERT INTO lookup_runs(
                     run_id,metric_version,selected_count,unique_count,
                     eligible_count,created
                   ) VALUES(?,?,?,?,?,?) ON CONFLICT(run_id) DO NOTHING""",
                (
                    normalized_run_id, str(cohort.get("metric_version") or ""),
                    max(0, int(cohort.get("selected_count") or 0)),
                    max(0, int(cohort.get("unique_count") or 0)),
                    max(0, int(cohort.get("eligible_count") or 0)), now,
                ),
            )
            for item in cohort.get("items") or []:
                connection.execute(
                    """INSERT INTO lookup_run_items(
                         run_id,source_identity,candidate_id,phase,eligible,
                         outcome,found,cached,trace,created
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,source_identity,phase) DO NOTHING""",
                    (
                        normalized_run_id, item["source_identity"],
                        int(item.get("candidate_id") or 0), "input",
                        1 if item.get("eligible") else 0,
                        "eligible" if item.get("eligible") else "input_ineligible",
                        0, 0,
                        json.dumps({
                            "ineligibility_reason": str(
                                item.get("ineligibility_reason") or ""
                            )[:120],
                        }),
                        now,
                    ),
                )


def finish_lookup_run(run_id: str, classified_items: list[dict]):
    """Append one final, contact-free outcome for every frozen identity."""
    now = time.time()
    normalized_run_id = str(run_id or "").strip()
    with _conn() as connection:
        with connection.transaction():
            for item in classified_items or []:
                connection.execute(
                    """INSERT INTO lookup_run_items(
                         run_id,source_identity,candidate_id,phase,eligible,
                         outcome,found,cached,trace,created
                       ) VALUES(?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(run_id,source_identity,phase) DO NOTHING""",
                    (
                        normalized_run_id, item["source_identity"],
                        int(item.get("candidate_id") or 0), "final",
                        1 if item.get("eligible") else 0,
                        str(item.get("outcome") or "backend_missing_result")[:120],
                        1 if item.get("found") else 0,
                        1 if item.get("cached") else 0,
                        json.dumps(item.get("trace") or {}), now,
                    ),
                )


def lookup_run_items(run_id: str, phase="final") -> list[dict]:
    with _conn() as connection:
        rows = connection.execute(
            """SELECT run_id,source_identity,candidate_id,phase,eligible,
                      outcome,found,cached,trace,created
               FROM lookup_run_items WHERE run_id=? AND phase=?
               ORDER BY id""",
            (str(run_id or "").strip(), str(phase or "final").strip()),
        ).fetchall()
    output = []
    for row in rows:
        item = dict(row)
        try:
            item["trace"] = json.loads(item.get("trace") or "{}")
        except Exception:
            item["trace"] = {}
        item["eligible"] = bool(item.get("eligible"))
        item["found"] = bool(item.get("found"))
        item["cached"] = bool(item.get("cached"))
        output.append(item)
    return output


def save_provider_lookup(
    provider, run_id, candidate_id, request_key, status, result, credits_spent=0,
):
    now = time.time()
    encoded = json.dumps(result or {})
    with _conn() as connection:
        connection.execute(
            """INSERT INTO provider_lookups(
                 provider,run_id,candidate_id,request_key,status,result,
                 credits_spent,created,updated
               ) VALUES(?,?,?,?,?,?,?,?,?)
               ON CONFLICT(provider,request_key) DO UPDATE SET
                 run_id=excluded.run_id,
                 candidate_id=excluded.candidate_id,
                 status=excluded.status,
                 result=excluded.result,
                 credits_spent=excluded.credits_spent,
                 created=excluded.created,
                 updated=excluded.updated""",
            (
                provider.strip().lower(), run_id.strip(), candidate_id,
                request_key.strip(), status.strip(), encoded,
                max(0, int(credits_spent or 0)), now, now,
            ),
        )


def provider_run_credits(provider, run_id):
    with _conn() as connection:
        value = _scalar(connection.execute(
            """SELECT COALESCE(SUM(credits_spent),0) FROM provider_lookups
               WHERE provider=? AND run_id=?""",
            (provider.strip().lower(), run_id.strip()),
        ))
        return int(value or 0)


def provider_lookup_preflight(provider, run_id, candidate_id, request_key_for):
    """Fetch the candidate, identity cache, and run spend in two DB round trips."""
    normalized_provider = provider.strip().lower()
    normalized_run_id = run_id.strip()
    with _conn() as connection:
        candidate_row = connection.execute(
            "SELECT * FROM candidates WHERE id=?", (candidate_id,)
        ).fetchone()
        if not candidate_row:
            return {
                "candidate": None,
                "request_key": "",
                "cached": None,
                "run_credits": 0,
            }
        candidate = _row(candidate_row)
        request_key = str(request_key_for(candidate) or "").strip()
        row = connection.execute(
            """SELECT
                 pl.id AS lookup_id,
                 pl.provider AS lookup_provider,
                 pl.run_id AS lookup_run_id,
                 pl.candidate_id AS lookup_candidate_id,
                 pl.request_key AS lookup_request_key,
                 pl.status AS lookup_status,
                 pl.result AS lookup_result,
                 pl.credits_spent AS lookup_credits_spent,
                 pl.created AS lookup_created,
                 pl.updated AS lookup_updated,
                 COALESCE((
                   SELECT SUM(pr.credits_spent)
                   FROM provider_lookups pr
                   WHERE pr.provider=? AND pr.run_id=?
                 ), 0) AS run_credits
               FROM (SELECT 1 AS marker) base
               LEFT JOIN provider_lookups pl
                 ON pl.provider=? AND pl.request_key=?
               LIMIT 1""",
            (
                normalized_provider,
                normalized_run_id,
                normalized_provider,
                request_key,
            ),
        ).fetchone()

    data = dict(row or {})
    cached = None
    if data.get("lookup_id") is not None:
        try:
            cached_result = json.loads(data.get("lookup_result") or "{}")
        except Exception:
            cached_result = {}
        cached = {
            "id": data.get("lookup_id"),
            "provider": data.get("lookup_provider"),
            "run_id": data.get("lookup_run_id"),
            "candidate_id": data.get("lookup_candidate_id"),
            "request_key": data.get("lookup_request_key"),
            "status": data.get("lookup_status"),
            "result": cached_result,
            "credits_spent": data.get("lookup_credits_spent") or 0,
            "created": data.get("lookup_created"),
            "updated": data.get("lookup_updated"),
        }
    return {
        "candidate": candidate,
        "request_key": request_key,
        "cached": cached,
        "run_credits": int(data.get("run_credits") or 0),
    }


def finalize_provider_lookup(
    provider,
    run_id,
    candidate_id,
    request_key,
    status,
    result,
    credits_spent=0,
    *,
    candidate_updates=None,
):
    """Atomically persist a provider result and optional candidate update."""
    if not candidate_updates:
        save_provider_lookup(
            provider,
            run_id,
            candidate_id,
            request_key,
            status,
            result,
            credits_spent,
        )
        return None

    fields = dict(candidate_updates)
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"invalid candidate fields: {', '.join(sorted(unknown))}")
    for key in ("phones", "emails", "addresses", "verification", "identity_evidence"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])

    now = time.time()
    fields["updated"] = now
    assignments = ", ".join(f"{key}=?" for key in fields)
    lookup_args = (
        provider.strip().lower(),
        run_id.strip(),
        candidate_id,
        request_key.strip(),
        status.strip(),
        json.dumps(result or {}),
        max(0, int(credits_spent or 0)),
        now,
        now,
    )
    upsert_sql = """INSERT INTO provider_lookups(
          provider,run_id,candidate_id,request_key,status,result,
          credits_spent,created,updated
        ) VALUES(?,?,?,?,?,?,?,?,?)
        ON CONFLICT(provider,request_key) DO UPDATE SET
          run_id=excluded.run_id,
          candidate_id=excluded.candidate_id,
          status=excluded.status,
          result=excluded.result,
          credits_spent=excluded.credits_spent,
          created=excluded.created,
          updated=excluded.updated"""

    with _conn() as connection:
        if connection.postgres:
            row = connection.execute(
                f"""WITH saved AS (
                       {upsert_sql}
                       RETURNING 1
                     )
                     UPDATE candidates
                     SET {assignments}
                     WHERE id=? AND EXISTS (SELECT 1 FROM saved)
                     RETURNING *""",
                (*lookup_args, *fields.values(), candidate_id),
            ).fetchone()
        else:
            connection.execute(upsert_sql, lookup_args)
            connection.execute(
                f"UPDATE candidates SET {assignments} WHERE id=?",
                (*fields.values(), candidate_id),
            )
            row = connection.execute(
                "SELECT * FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
        return _row(row) if row else None


def provider_lookup_preflight_batch(provider, run_id, candidate_ids, request_key_for):
    """Preflight a whole lookup run in three round trips instead of two per candidate.

    A 50-card Indeed selection therefore reaches People Data Labs after three
    Neon queries rather than a hundred.
    """
    normalized_provider = provider.strip().lower()
    normalized_run_id = run_id.strip()
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    empty = {"candidates": {}, "request_keys": {}, "cached": {}, "run_credits": 0}
    if not ordered_ids:
        return empty

    id_placeholders = ",".join("?" for _ in ordered_ids)
    with _conn() as connection:
        candidates = {
            int(candidate["id"]): candidate
            for candidate in (
                _row(row) for row in connection.execute(
                    f"SELECT * FROM candidates WHERE id IN ({id_placeholders})",
                    ordered_ids,
                )
            )
        }
        request_keys = {
            cid: str(request_key_for(candidate) or "").strip()
            for cid, candidate in candidates.items()
        }
        keys = [key for key in dict.fromkeys(request_keys.values()) if key]
        cached = {}
        if keys:
            key_placeholders = ",".join("?" for _ in keys)
            for row in connection.execute(
                f"""SELECT * FROM provider_lookups
                    WHERE provider=? AND request_key IN ({key_placeholders})""",
                (normalized_provider, *keys),
            ):
                entry = dict(row)
                try:
                    entry["result"] = json.loads(entry.get("result") or "{}")
                except Exception:
                    entry["result"] = {}
                cached[str(entry.get("request_key") or "")] = entry
        run_credits = int(_scalar(connection.execute(
            """SELECT COALESCE(SUM(credits_spent),0) FROM provider_lookups
               WHERE provider=? AND run_id=?""",
            (normalized_provider, normalized_run_id),
        )) or 0)

    return {
        "candidates": candidates,
        "request_keys": request_keys,
        "cached": cached,
        "run_credits": run_credits,
    }


# Columns a batched provider result may write. Anything else must go through
# ``update_candidate`` so the single-row validation still applies.
_BATCH_CANDIDATE_FIELDS = (
    "emails", "phones", "addresses", "enrich_status", "confidence",
    "verification", "stage", "canonical_name", "identity_status",
    "identity_score", "identity_provider", "provider_person_id",
    "master_candidate_id", "identity_evidence", "identity_verified_at", "contact_verified_at",
    "contact_expires_at",
)


def _batch_update_value(field, value):
    if field in ("phones", "emails", "addresses", "verification", "identity_evidence"):
        return value if isinstance(value, str) else json.dumps(value)
    if field in (
        "confidence", "identity_score", "identity_verified_at",
        "contact_verified_at", "contact_expires_at",
    ):
        return float(value or 0)
    return value


def finalize_provider_lookups(provider, run_id, entries):
    """Persist a whole run's provider results and candidate updates atomically.

    ``entries`` items carry ``candidate_id``, ``request_key``, ``status``,
    ``result``, ``credits_spent``, and an optional ``candidate_updates`` dict.
    Returns the refreshed candidate rows keyed by candidate id.
    """
    normalized_provider = provider.strip().lower()
    normalized_run_id = run_id.strip()
    now = time.time()

    # One provider_lookups row per request key, one UPDATE per candidate: a
    # repeated identity inside a single run must not conflict with itself.
    lookups, updates = {}, {}
    for entry in entries or []:
        request_key = str(entry.get("request_key") or "").strip()
        candidate_id = int(entry["candidate_id"])
        credits = max(0, int(entry.get("credits_spent") or 0))
        # ``ON CONFLICT`` cannot touch the same row twice in one statement, and
        # the surviving row must keep the credit the provider actually charged.
        existing = lookups.get(request_key)
        if request_key and (existing is None or credits >= existing[4]):
            lookups[request_key] = (
                candidate_id,
                request_key,
                str(entry.get("status") or "").strip(),
                json.dumps(entry.get("result") or {}),
                credits,
                now,
                now,
            )
        fields = dict(entry.get("candidate_updates") or {})
        if not fields:
            continue
        unknown = set(fields) - set(_BATCH_CANDIDATE_FIELDS)
        if unknown:
            raise ValueError(f"invalid candidate fields: {', '.join(sorted(unknown))}")
        updates[candidate_id] = fields
    if not lookups and not updates:
        return {}

    lookup_rows = list(lookups.values())
    update_rows = [
        (
            candidate_id,
            *(
                _batch_update_value(field, fields[field])
                if field in fields
                else None
                for field in _BATCH_CANDIDATE_FIELDS
            ),
            now,
        )
        for candidate_id, fields in updates.items()
    ]

    refreshed = {}
    with _conn() as connection:
        with connection.transaction():
            if connection.postgres:
                if lookup_rows:
                    values_sql = ",".join(
                        "(?::BIGINT,?::TEXT,?::TEXT,?::TEXT,?::INTEGER,"
                        "?::DOUBLE PRECISION,?::DOUBLE PRECISION)"
                        for _ in lookup_rows
                    )
                    connection.execute(
                        f"""INSERT INTO provider_lookups(
                              provider,run_id,candidate_id,request_key,status,
                              result,credits_spent,created,updated
                            )
                            SELECT ?,?,v.candidate_id,v.request_key,v.status,
                                   v.result,v.credits_spent,v.created,v.updated
                            FROM (VALUES {values_sql}) AS v(
                              candidate_id,request_key,status,result,
                              credits_spent,created,updated
                            )
                            ON CONFLICT(provider,request_key) DO UPDATE SET
                              run_id=excluded.run_id,
                              candidate_id=excluded.candidate_id,
                              status=excluded.status,
                              result=excluded.result,
                              credits_spent=excluded.credits_spent,
                              created=excluded.created,
                              updated=excluded.updated""",
                        (
                            normalized_provider,
                            normalized_run_id,
                            *(value for row in lookup_rows for value in row),
                        ),
                    )
                if update_rows:
                    values_sql = ",".join(
                        "(?::BIGINT,?::TEXT,?::TEXT,?::TEXT,?::TEXT,"
                        "?::DOUBLE PRECISION,?::TEXT,?::TEXT,?::TEXT,?::TEXT,"
                        "?::DOUBLE PRECISION,?::TEXT,?::TEXT,?::BIGINT,?::TEXT,"
                        "?::DOUBLE PRECISION,?::DOUBLE PRECISION,"
                        "?::DOUBLE PRECISION,?::DOUBLE PRECISION)"
                        for _ in update_rows
                    )
                    # NULL means "leave the stored value alone" for every column.
                    rows = connection.execute(
                        f"""UPDATE candidates c
                            SET emails=COALESCE(v.emails,c.emails),
                                phones=COALESCE(v.phones,c.phones),
                                addresses=COALESCE(v.addresses,c.addresses),
                                enrich_status=COALESCE(v.enrich_status,c.enrich_status),
                                confidence=COALESCE(v.confidence,c.confidence),
                                verification=COALESCE(v.verification,c.verification),
                                stage=COALESCE(v.stage,c.stage),
                                canonical_name=COALESCE(v.canonical_name,c.canonical_name),
                                identity_status=COALESCE(v.identity_status,c.identity_status),
                                identity_score=COALESCE(v.identity_score,c.identity_score),
                                identity_provider=COALESCE(v.identity_provider,c.identity_provider),
                                provider_person_id=COALESCE(v.provider_person_id,c.provider_person_id),
                                master_candidate_id=COALESCE(v.master_candidate_id,c.master_candidate_id),
                                identity_evidence=COALESCE(v.identity_evidence,c.identity_evidence),
                                identity_verified_at=COALESCE(v.identity_verified_at,c.identity_verified_at),
                                contact_verified_at=COALESCE(v.contact_verified_at,c.contact_verified_at),
                                contact_expires_at=COALESCE(v.contact_expires_at,c.contact_expires_at),
                                updated=v.updated
                            FROM (VALUES {values_sql}) AS v(
                              candidate_id,emails,phones,addresses,enrich_status,
                              confidence,verification,stage,canonical_name,
                              identity_status,identity_score,identity_provider,
                              provider_person_id,master_candidate_id,identity_evidence,
                              identity_verified_at,contact_verified_at,
                              contact_expires_at,updated
                            )
                            WHERE c.id=v.candidate_id
                            RETURNING c.*""",
                        tuple(value for row in update_rows for value in row),
                    ).fetchall()
                    refreshed = {int(row["id"]): _row(row) for row in rows}
                return refreshed

            for row in lookup_rows:
                connection.execute(
                    """INSERT INTO provider_lookups(
                         provider,run_id,candidate_id,request_key,status,result,
                         credits_spent,created,updated
                       ) VALUES(?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(provider,request_key) DO UPDATE SET
                         run_id=excluded.run_id,
                         candidate_id=excluded.candidate_id,
                         status=excluded.status,
                         result=excluded.result,
                         credits_spent=excluded.credits_spent,
                         created=excluded.created,
                         updated=excluded.updated""",
                    (normalized_provider, normalized_run_id, *row),
                )
            for candidate_id, fields in updates.items():
                assignments = {
                    field: _batch_update_value(field, value)
                    for field, value in fields.items()
                }
                assignments["updated"] = now
                clause = ", ".join(f"{field}=?" for field in assignments)
                connection.execute(
                    f"UPDATE candidates SET {clause} WHERE id=?",
                    (*assignments.values(), candidate_id),
                )
                row = connection.execute(
                    "SELECT * FROM candidates WHERE id=?", (candidate_id,)
                ).fetchone()
                if row:
                    refreshed[candidate_id] = _row(row)
    return refreshed


def upsert_candidate_profiles(profiles, default_job_id=None):
    """Insert or refresh captured profiles in one database transaction."""
    now = time.time()
    prepared = []
    for profile in profiles:
        prepared.append({
            "name": (profile.get("name") or "").strip(),
            "location": (profile.get("location") or "").strip(),
            "hometown": (profile.get("hometown") or "").strip(),
            "source": (profile.get("source") or "indeed").strip().lower(),
            "source_id": (profile.get("source_id") or "").strip(),
            "source_url": (profile.get("source_url") or "").strip(),
            "notes": (profile.get("notes") or "").strip(),
            "job_id": (
                profile.get("job_id")
                if profile.get("job_id") is not None
                else default_job_id
            ),
        })

    # Resolve, update, insert, and return the entire PostgreSQL batch in one
    # set-based statement. A 50-card Indeed scan therefore uses one Neon round
    # trip instead of executing 100-200 individual statements.
    if config.DATABASE_URL and prepared:
        unique_profiles = []
        canonical_indexes = []
        source_ids = {}
        identities = {}
        for profile in prepared:
            source_key = (
                (profile["source"].casefold(), profile["source_id"].casefold())
                if profile["source_id"]
                else None
            )
            identity_key = (
                profile["source"].casefold(),
                profile["name"].casefold(),
                profile["location"].casefold(),
            )
            # A source-owned identifier is the authoritative identity boundary.
            # Never merge a profile carrying one non-empty source_id into a
            # same-name/location row carrying a different source_id.  The
            # weaker identity fallback is only suitable for captures where the
            # source did not provide an identifier at all.
            canonical = (
                source_ids.get(source_key)
                if source_key is not None
                else identities.get(identity_key)
            )
            if canonical is None:
                canonical = len(unique_profiles)
                unique_profiles.append(dict(profile))
                if source_key:
                    source_ids[source_key] = canonical
                identities.setdefault(identity_key, canonical)
            else:
                merged = unique_profiles[canonical]
                if "Specialty:" in profile["notes"] and profile["notes"] not in merged["notes"]:
                    merged["notes"] = (profile["notes"] + "\n" + merged["notes"])[:20000]
                elif len(profile["notes"]) > len(merged["notes"]):
                    merged["notes"] = profile["notes"]
                if profile["source_url"]:
                    merged["source_url"] = profile["source_url"]
                if profile["location"] and not merged["location"]:
                    merged["location"] = profile["location"]
                if profile["hometown"]:
                    merged["hometown"] = profile["hometown"]
                if profile["source_id"] and not merged["source_id"]:
                    merged["source_id"] = profile["source_id"]
                if profile["job_id"] is not None and merged["job_id"] is None:
                    merged["job_id"] = profile["job_id"]
            canonical_indexes.append(canonical)

        value_sql = ",".join(
            "(?::INTEGER,?::TEXT,?::TEXT,?::TEXT,?::BIGINT,?::TEXT,?::TEXT,"
            "?::TEXT,?::TEXT,?::DOUBLE PRECISION,?::DOUBLE PRECISION)"
            for _ in unique_profiles
        )
        args = []
        for ordinal, profile in enumerate(unique_profiles):
            args.extend((
                ordinal,
                profile["name"],
                profile["location"],
                profile["hometown"],
                profile["job_id"],
                profile["notes"],
                profile["source"],
                profile["source_url"],
                profile["source_id"],
                now,
                now,
            ))

        query = f"""WITH input_rows(
              ordinal,name,location,hometown,job_id,notes,source,source_url,source_id,
              created,updated
            ) AS (VALUES {value_sql}),
            matched AS MATERIALIZED (
              SELECT i.*,
                     (
                       SELECT c.id
                       FROM candidates c
                       WHERE (
                         i.source_id<>'' AND c.source=i.source
                         AND c.source_id=i.source_id
                       ) OR (
                         i.source_id=''
                         AND
                         LOWER(c.source)=LOWER(i.source)
                         AND LOWER(c.name)=LOWER(i.name)
                         AND LOWER(COALESCE(c.location,''))=LOWER(i.location)
                       )
                       ORDER BY CASE
                         WHEN i.source_id<>'' AND c.source=i.source
                              AND c.source_id=i.source_id THEN 0
                         ELSE 1
                       END, c.created DESC
                       LIMIT 1
                     ) AS existing_id
              FROM input_rows i
            ),
            updated_rows AS (
              UPDATE candidates c
              SET updated=m.updated,
                  name=CASE
                    WHEN m.source_id<>'' AND c.source=m.source
                         AND c.source_id=m.source_id AND m.name<>'' THEN m.name
                    ELSE c.name
                  END,
                  notes=CASE
                    WHEN POSITION('Specialty:' IN m.notes)>0
                      AND POSITION(m.notes IN COALESCE(c.notes,''))=0
                    THEN LEFT(m.notes || CHR(10) || COALESCE(c.notes,''), 20000)
                    WHEN LENGTH(m.notes)>LENGTH(COALESCE(c.notes,'')) THEN m.notes
                    ELSE c.notes
                  END,
                  location=CASE
                    WHEN m.source_id<>'' AND c.source=m.source
                         AND c.source_id=m.source_id AND m.location<>'' THEN m.location
                    WHEN COALESCE(c.location,'')='' AND m.location<>'' THEN m.location
                    ELSE c.location
                  END,
                  hometown=CASE
                    WHEN m.source_id<>'' AND c.source=m.source
                         AND c.source_id=m.source_id AND m.hometown<>'' THEN m.hometown
                    WHEN COALESCE(c.hometown,'')='' AND m.hometown<>'' THEN m.hometown
                    ELSE c.hometown
                  END,
                  emails=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '[]' ELSE c.emails END,
                  phones=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '[]' ELSE c.phones END,
                  addresses=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '[]' ELSE c.addresses END,
                  enrich_status=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 'pending' ELSE c.enrich_status END,
                  confidence=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 0 ELSE c.confidence END,
                  verification=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '{{}}' ELSE c.verification END,
                  canonical_name=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '' ELSE c.canonical_name END,
                  identity_status=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 'captured' ELSE c.identity_status END,
                  identity_score=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 0 ELSE c.identity_score END,
                  identity_provider=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '' ELSE c.identity_provider END,
                  provider_person_id=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '' ELSE c.provider_person_id END,
                  master_candidate_id=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN NULL ELSE c.master_candidate_id END,
                  identity_evidence=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN '{{}}' ELSE c.identity_evidence END,
                  identity_verified_at=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 0 ELSE c.identity_verified_at END,
                  contact_verified_at=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 0 ELSE c.contact_verified_at END,
                  contact_expires_at=CASE WHEN (
                    m.source_id<>'' AND c.source=m.source AND c.source_id=m.source_id
                    AND (
                      (m.name<>'' AND LOWER(TRIM(m.name))<>LOWER(TRIM(COALESCE(c.name,''))))
                      OR (m.location<>'' AND LOWER(TRIM(m.location))<>LOWER(TRIM(COALESCE(c.location,''))))
                    )
                  ) THEN 0 ELSE c.contact_expires_at END,
                  source_url=CASE
                    WHEN m.source_url<>'' THEN m.source_url ELSE c.source_url
                  END,
                  source_id=CASE
                    WHEN COALESCE(c.source_id,'')='' AND m.source_id<>'' THEN m.source_id
                    ELSE c.source_id
                  END,
                  job_id=COALESCE(c.job_id,m.job_id)
              FROM matched m
              WHERE c.id=m.existing_id
              RETURNING m.ordinal AS input_ordinal, FALSE AS imported, c.*
            ),
            new_rows AS MATERIALIZED (
              SELECT m.*,
                     nextval(pg_get_serial_sequence('candidates','id'))::BIGINT AS new_id
              FROM matched m
              WHERE m.existing_id IS NULL
            ),
            inserted_rows AS (
              INSERT INTO candidates(
                id,name,location,hometown,job_id,notes,source,source_url,source_id,
                created,updated
              )
              SELECT new_id,name,location,hometown,job_id,notes,source,source_url,source_id,
                     created,updated
              FROM new_rows
              RETURNING *
            ),
            changed AS (
              SELECT * FROM updated_rows
              UNION ALL
              SELECT n.ordinal AS input_ordinal, TRUE AS imported, i.*
              FROM new_rows n
              JOIN inserted_rows i ON i.id=n.new_id
            )
            SELECT * FROM changed ORDER BY input_ordinal"""

        with _conn() as connection:
            rows = [dict(row) for row in connection.execute(query, args)]
        unique_results = []
        for row in rows:
            imported = bool(row.pop("imported"))
            row.pop("input_ordinal", None)
            candidate = _row(row)
            unique_results.append({
                "id": candidate["id"],
                "imported": imported,
                "candidate": candidate,
            })
        return [unique_results[index] for index in canonical_indexes]

    results = []
    with _conn() as connection:
        for profile in prepared:
            name = profile["name"]
            location = profile["location"]
            hometown = profile["hometown"]
            source = profile["source"]
            source_id = profile["source_id"]
            source_url = profile["source_url"]
            notes = profile["notes"]
            job_id = profile["job_id"]

            existing_row = None
            matched_by_source_id = False
            if source_id:
                existing_row = connection.execute(
                    """SELECT * FROM candidates
                       WHERE source=? AND source_id=?
                       ORDER BY created DESC LIMIT 1""",
                    (source, source_id),
                ).fetchone()
                matched_by_source_id = existing_row is not None
            if existing_row is None and not source_id:
                existing_row = connection.execute(
                    """SELECT * FROM candidates
                       WHERE LOWER(source)=LOWER(?) AND LOWER(name)=LOWER(?)
                         AND LOWER(COALESCE(location,''))=LOWER(?)
                       ORDER BY created DESC LIMIT 1""",
                    (source, name, location),
                ).fetchone()

            if existing_row is not None:
                existing = _row(existing_row)
                updates = {"updated": now}
                identity_changed = bool(
                    matched_by_source_id and (
                        (name and name.casefold() != str(existing.get("name") or "").strip().casefold())
                        or (
                            location
                            and location.casefold()
                            != str(existing.get("location") or "").strip().casefold()
                        )
                    )
                )
                if matched_by_source_id and name and name != existing.get("name"):
                    updates["name"] = name
                existing_notes = existing.get("notes") or ""
                if notes and "Specialty:" in notes and notes not in existing_notes:
                    updates["notes"] = (notes + "\n" + existing_notes)[:20000]
                elif notes and len(notes) > len(existing_notes):
                    updates["notes"] = notes
                if location and (matched_by_source_id or not existing.get("location")):
                    updates["location"] = location
                if hometown and (matched_by_source_id or not existing.get("hometown")):
                    updates["hometown"] = hometown
                if identity_changed:
                    updates.update({
                        "emails": "[]", "phones": "[]", "addresses": "[]",
                        "enrich_status": "pending", "confidence": 0,
                        "verification": "{}", "canonical_name": "",
                        "identity_status": "captured", "identity_score": 0,
                        "identity_provider": "", "provider_person_id": "",
                        "master_candidate_id": None, "identity_evidence": "{}",
                        "identity_verified_at": 0, "contact_verified_at": 0,
                        "contact_expires_at": 0,
                    })
                if source_url:
                    updates["source_url"] = source_url
                if source_id and not existing.get("source_id"):
                    updates["source_id"] = source_id
                if job_id is not None and existing.get("job_id") is None:
                    updates["job_id"] = job_id
                assignments = ", ".join(f"{key}=?" for key in updates)
                connection.execute(
                    f"UPDATE candidates SET {assignments} WHERE id=?",
                    (*updates.values(), existing["id"]),
                )
                refreshed = connection.execute(
                    "SELECT * FROM candidates WHERE id=?", (existing["id"],)
                ).fetchone()
                results.append({
                    "id": existing["id"],
                    "imported": False,
                    "candidate": _row(refreshed),
                })
                continue

            candidate_id = _insert_id(
                connection,
                """INSERT INTO candidates(
                     name,location,hometown,job_id,notes,source,source_url,source_id,created,updated
                   ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (name, location, hometown, job_id, notes, source, source_url, source_id, now, now),
            )
            inserted = connection.execute(
                "SELECT * FROM candidates WHERE id=?", (candidate_id,)
            ).fetchone()
            results.append({
                "id": candidate_id,
                "imported": True,
                "candidate": _row(inserted),
            })
    return results


def update_candidate(candidate_id, **fields):
    if not fields:
        return
    unknown = set(fields) - _CANDIDATE_FIELDS
    if unknown:
        raise ValueError(f"invalid candidate fields: {', '.join(sorted(unknown))}")
    for key in ("phones", "emails", "addresses", "verification", "identity_evidence"):
        if key in fields and not isinstance(fields[key], str):
            fields[key] = json.dumps(fields[key])
    fields["updated"] = time.time()
    assignments = ", ".join(f"{key}=?" for key in fields)
    with _conn() as connection:
        with connection.transaction():
            connection.execute(
                f"UPDATE candidates SET {assignments} WHERE id=?",
                (*fields.values(), candidate_id),
            )
            if "master_candidate_id" in fields:
                _rekey_nexus_identity(connection, int(candidate_id))


def set_stage(candidate_id, stage):
    if stage not in PIPELINE_STAGES:
        raise ValueError(f"invalid stage {stage}")
    update_candidate(candidate_id, stage=stage)


# ---- talent pools / campaigns ----
def list_talent_pools():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT p.*, COUNT(m.candidate_id) AS candidate_count
               FROM talent_pools p
               LEFT JOIN talent_pool_members m ON m.pool_id=p.id
               GROUP BY p.id, p.name, p.created, p.updated
               ORDER BY p.updated DESC, p.created DESC"""
        )]


def add_candidates_to_pool(name, candidate_ids):
    normalized_name = str(name or "").strip()[:200]
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    if not normalized_name:
        raise ValueError("pool name is required")
    if not ordered_ids:
        raise ValueError("at least one candidate is required")

    now = time.time()
    placeholders = ",".join("?" for _ in ordered_ids)
    with _conn() as connection:
        with connection.transaction():
            valid_ids = [
                int(row["id"] if isinstance(row, (dict, sqlite3.Row)) else row[0])
                for row in connection.execute(
                    f"SELECT id FROM candidates WHERE id IN ({placeholders})",
                    ordered_ids,
                )
            ]
            if not valid_ids:
                raise ValueError("no saved candidates were found")
            row = connection.execute(
                "SELECT id FROM talent_pools WHERE LOWER(name)=LOWER(?) LIMIT 1",
                (normalized_name,),
            ).fetchone()
            if row:
                pool_id = int(row["id"] if isinstance(row, (dict, sqlite3.Row)) else row[0])
                connection.execute(
                    "UPDATE talent_pools SET updated=? WHERE id=?", (now, pool_id)
                )
            else:
                pool_id = _insert_id(
                    connection,
                    "INSERT INTO talent_pools(name,created,updated) VALUES(?,?,?)",
                    (normalized_name, now, now),
                )
            before = int(_scalar(connection.execute(
                "SELECT COUNT(*) FROM talent_pool_members WHERE pool_id=?", (pool_id,)
            )) or 0)
            for candidate_id in valid_ids:
                connection.execute(
                    """INSERT INTO talent_pool_members(pool_id,candidate_id,created)
                       VALUES(?,?,?) ON CONFLICT(pool_id,candidate_id) DO NOTHING""",
                    (pool_id, candidate_id, now),
                )
            total = int(_scalar(connection.execute(
                "SELECT COUNT(*) FROM talent_pool_members WHERE pool_id=?", (pool_id,)
            )) or 0)
    return {
        "id": pool_id,
        "name": normalized_name,
        "added": max(0, total - before),
        "already_present": max(0, len(valid_ids) - (total - before)),
        "candidate_count": total,
    }


def list_campaigns():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            """SELECT c.*, COUNT(m.candidate_id) AS candidate_count
               FROM campaigns c
               LEFT JOIN campaign_members m ON m.campaign_id=c.id
               GROUP BY c.id, c.name, c.job_id, c.status, c.created, c.updated
               ORDER BY c.updated DESC, c.created DESC"""
        )]


def create_campaign(name, candidate_ids, job_id=None):
    normalized_name = str(name or "").strip()[:200]
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    if not normalized_name:
        raise ValueError("campaign name is required")
    if not ordered_ids:
        raise ValueError("at least one candidate is required")

    now = time.time()
    placeholders = ",".join("?" for _ in ordered_ids)
    with _conn() as connection:
        with connection.transaction():
            valid_ids = [
                int(row["id"] if isinstance(row, (dict, sqlite3.Row)) else row[0])
                for row in connection.execute(
                    f"SELECT id FROM candidates WHERE id IN ({placeholders})",
                    ordered_ids,
                )
            ]
            if not valid_ids:
                raise ValueError("no saved candidates were found")
            campaign_id = _insert_id(
                connection,
                """INSERT INTO campaigns(name,job_id,status,created,updated)
                   VALUES(?,?,'draft',?,?)""",
                (normalized_name, job_id, now, now),
            )
            for candidate_id in valid_ids:
                connection.execute(
                    """INSERT INTO campaign_members(
                         campaign_id,candidate_id,status,created,updated
                       ) VALUES(?,?,'queued',?,?)
                       ON CONFLICT(campaign_id,candidate_id) DO NOTHING""",
                    (campaign_id, candidate_id, now, now),
                )
    return {
        "id": campaign_id,
        "name": normalized_name,
        "status": "draft",
        "job_id": job_id,
        "candidate_count": len(valid_ids),
    }


# ---- Nexus delivery outbox ----
def _rekey_nexus_identity(connection, candidate_id: int) -> str:
    """Migrate pre-existing outbox/link keys after identity consolidation."""
    canonical = _nexus_identity_key(connection, candidate_id)
    delivery_keys = {
        str(row["identity_key"])
        for row in connection.execute(
            "SELECT DISTINCT identity_key FROM nexus_deliveries WHERE candidate_id=?",
            (int(candidate_id),),
        ).fetchall()
        if str(row["identity_key"] or "").strip()
    }
    link_keys = {
        str(row["identity_key"])
        for row in connection.execute(
            "SELECT identity_key FROM nexus_candidate_links WHERE candidate_id=?",
            (int(candidate_id),),
        ).fetchall()
        if str(row["identity_key"] or "").strip()
    }
    old_keys = (delivery_keys | link_keys | {
        f"candidate:{int(candidate_id)}", f"master:{int(candidate_id)}",
    }) - {canonical}
    if not old_keys:
        return canonical

    target = connection.execute(
        "SELECT nexus_candidate_id FROM nexus_candidate_links WHERE identity_key=?",
        (canonical,),
    ).fetchone()
    target_id = str(target["nexus_candidate_id"]) if target else ""
    conflict = False
    for old_key in sorted(old_keys):
        old = connection.execute(
            "SELECT nexus_candidate_id FROM nexus_candidate_links WHERE identity_key=?",
            (old_key,),
        ).fetchone()
        if not old:
            continue
        old_id = str(old["nexus_candidate_id"])
        if target_id and old_id != target_id:
            conflict = True
            continue
        if target_id and old_id == target_id:
            connection.execute(
                "DELETE FROM nexus_candidate_links WHERE identity_key=?", (old_key,),
            )
            continue
        connection.execute(
            """UPDATE nexus_candidate_links
               SET identity_key=?,candidate_id=?,updated=? WHERE identity_key=?""",
            (canonical, int(candidate_id), time.time(), old_key),
        )
        target_id = old_id

    placeholders = ",".join("?" for _ in old_keys)
    args = [int(candidate_id), *sorted(old_keys)]
    if conflict:
        connection.execute(
            f"""UPDATE nexus_deliveries
                SET status=CASE WHEN status='writing' THEN 'indeterminate' ELSE 'review' END,
                    lease_until=0,operation='identity_rekey',
                    last_error='nexus_identity_link_conflict: manual reconciliation required',
                    updated=?
                WHERE candidate_id=? AND identity_key IN ({placeholders})
                  AND status IN ('pending','retry','processing','writing')""",
            (time.time(), *args),
        )
        return canonical

    # Stop an in-flight worker before changing its serialization key. Its
    # fenced acknowledgement will then fail instead of mutating the new link.
    connection.execute(
        f"""UPDATE nexus_deliveries
            SET status=CASE WHEN status='writing' THEN 'indeterminate' ELSE 'review' END,
                lease_until=0,operation='identity_rekey',
                last_error='nexus_identity_changed: reconciliation required',updated=?
            WHERE candidate_id=? AND identity_key IN ({placeholders})
              AND status IN ('processing','writing')""",
        (time.time(), *args),
    )
    connection.execute(
        f"""UPDATE nexus_deliveries SET identity_key=?,updated=?
            WHERE candidate_id=? AND identity_key IN ({placeholders})""",
        (canonical, time.time(), *args),
    )
    return canonical


def _migrate_all_nexus_identity_keys(connection) -> None:
    rows = connection.execute(
        """SELECT DISTINCT legacy.candidate_id
           FROM (
             SELECT d.candidate_id
             FROM nexus_deliveries d
             JOIN candidates c ON c.id=d.candidate_id
             WHERE d.identity_key <>
                   'master:' || CAST(COALESCE(c.master_candidate_id,c.id) AS TEXT)
             UNION
             SELECT l.candidate_id
             FROM nexus_candidate_links l
             JOIN candidates c ON c.id=l.candidate_id
             WHERE l.identity_key <>
                   'master:' || CAST(COALESCE(c.master_candidate_id,c.id) AS TEXT)
           ) legacy"""
    ).fetchall()
    for row in rows:
        _rekey_nexus_identity(connection, int(row["candidate_id"]))


def _nexus_identity_key(connection, candidate_id: int) -> str:
    row = connection.execute(
        "SELECT id,master_candidate_id FROM candidates WHERE id=?",
        (int(candidate_id),),
    ).fetchone()
    if not row:
        raise ValueError("candidate not found")
    # The root candidate and every later duplicate must use exactly the same
    # namespace. ``candidate:<id>`` for the root and ``master:<id>`` for a
    # child would permit two concurrent Nexus links for one resolved person.
    master_id = row["master_candidate_id"] or row["id"]
    return f"master:{int(master_id)}"


def _enqueue_nexus_delivery(connection, candidate_id, resume_id, resume_checksum="", requested_by=""):
    identity_key = _nexus_identity_key(connection, int(candidate_id))
    now = time.time()
    connection.execute(
        """INSERT INTO nexus_deliveries(
             candidate_id,resume_id,identity_key,resume_checksum,requested_by,status,
             attempts,next_attempt_at,lease_until,created,updated
           ) VALUES(?,?,?,?,?,'pending',0,0,0,?,?)
           ON CONFLICT(resume_id) DO NOTHING""",
        (
            int(candidate_id), int(resume_id), identity_key,
            str(resume_checksum or "").strip().lower(), str(requested_by or "")[:200], now, now,
        ),
    )
    return identity_key


def enqueue_nexus_delivery(candidate_id, resume_id, resume_checksum="", requested_by=""):
    """Idempotently queue one stored resume for backend-only Nexus delivery."""
    with _conn() as connection:
        with connection.transaction():
            _enqueue_nexus_delivery(
                connection, candidate_id, resume_id, resume_checksum, requested_by,
            )
    return get_nexus_delivery_for_resume(resume_id)


def get_nexus_delivery_for_resume(resume_id):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM nexus_deliveries WHERE resume_id=?",
            (int(resume_id),),
        ).fetchone()
        return dict(row) if row else None


def claim_nexus_delivery(lease_seconds=90):
    """Lease one ready delivery so several installed backends cannot duplicate it."""
    now = time.time()
    lease_until = now + max(10.0, float(lease_seconds or 90))
    with _conn() as connection:
        with connection.transaction():
            # Once a worker crossed the explicit pre-write boundary, an
            # expired lease has an unknowable upstream outcome. Never replay
            # it automatically after a crash.
            connection.execute(
                """UPDATE nexus_deliveries
                   SET status='indeterminate',lease_until=0,
                       operation='write_outcome',
                       last_error='nexus_write_outcome_unknown: manual reconciliation required',
                       updated=?
                   WHERE status='writing' AND lease_until<=?""",
                (now, now),
            )
            if connection.postgres:
                row = connection.execute(
                    """WITH picked AS (
                       SELECT d.id FROM nexus_deliveries d
                       WHERE (
                           (d.status IN ('pending','retry') AND d.next_attempt_at<=?)
                           OR (d.status='processing' AND d.lease_until<=?)
                         )
                         AND NOT EXISTS (
                           SELECT 1 FROM nexus_deliveries active
                           WHERE active.identity_key=d.identity_key
                             AND active.id<>d.id
                             AND active.status IN ('processing','writing')
                         )
                         ORDER BY CASE WHEN d.status='processing' THEN 0 ELSE 1 END,
                                  d.created,d.id
                         FOR UPDATE SKIP LOCKED
                         LIMIT 1
                       )
                       UPDATE nexus_deliveries d
                       SET status='processing',attempts=d.attempts+1,
                           lease_until=?,updated=?
                       FROM picked
                       WHERE d.id=picked.id
                       RETURNING d.*""",
                    (now, now, lease_until, now),
                ).fetchone()
                return dict(row) if row else None

            row = connection.execute(
                """SELECT d.* FROM nexus_deliveries d
                   WHERE (
                     (d.status IN ('pending','retry') AND d.next_attempt_at<=?)
                     OR (d.status='processing' AND d.lease_until<=?)
                   ) AND NOT EXISTS (
                     SELECT 1 FROM nexus_deliveries active
                     WHERE active.identity_key=d.identity_key
                       AND active.id<>d.id
                       AND active.status IN ('processing','writing')
                   )
                   ORDER BY CASE WHEN d.status='processing' THEN 0 ELSE 1 END,
                            d.created,d.id LIMIT 1""",
                (now, now),
            ).fetchone()
            if not row:
                return None
            updated = connection.execute(
                """UPDATE nexus_deliveries
                   SET status='processing',attempts=attempts+1,lease_until=?,updated=?
                   WHERE id=? AND (
                     (status IN ('pending','retry') AND next_attempt_at<=?)
                     OR (status='processing' AND lease_until<=?)
                   )""",
                (lease_until, now, int(row["id"]), now, now),
            )
            if updated.rowcount != 1:
                return None
            claimed = connection.execute(
                "SELECT * FROM nexus_deliveries WHERE id=?", (int(row["id"]),)
            ).fetchone()
            return dict(claimed) if claimed else None


def mark_nexus_delivery_writing(delivery_id, *, expected_lease_until, operation):
    """Fence one claimed row immediately before its first upstream mutation."""
    with _conn() as connection:
        updated = connection.execute(
            """UPDATE nexus_deliveries
               SET status='writing',operation=?,updated=?
               WHERE id=? AND status='processing' AND lease_until=?""",
            (
                str(operation or "write")[:80], time.time(), int(delivery_id),
                float(expected_lease_until),
            ),
        )
        return updated.rowcount == 1


def finish_nexus_delivery(
    delivery_id, status, *, nexus_candidate_id="", operation="", error="",
    retry_at=0, expected_lease_until=None,
):
    allowed = {"succeeded", "retry", "review", "failed", "indeterminate"}
    normalized = str(status or "").strip().lower()
    if normalized not in allowed:
        raise ValueError("invalid Nexus delivery status")
    now = time.time()
    with _conn() as connection:
        where = "WHERE id=?"
        args = [
            normalized, max(0.0, float(retry_at or 0)),
            str(nexus_candidate_id or "")[:120], str(operation or "")[:80],
            str(error or "")[:1000], now, int(delivery_id),
        ]
        if expected_lease_until is not None:
            where += " AND status IN ('processing','writing') AND lease_until=?"
            args.append(float(expected_lease_until))
        updated = connection.execute(
            """UPDATE nexus_deliveries
               SET status=?,next_attempt_at=?,lease_until=0,
                   nexus_candidate_id=?,operation=?,last_error=?,updated=?
               """ + where,
            tuple(args),
        )
        return updated.rowcount == 1


def get_nexus_candidate_link(identity_key):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM nexus_candidate_links WHERE identity_key=?",
            (str(identity_key or "").strip(),),
        ).fetchone()
        return dict(row) if row else None


def _save_nexus_candidate_link(
    connection, identity_key, candidate_id, nexus_candidate_id, *, now=None,
):
    identity = str(identity_key or "").strip()
    nexus_id = str(nexus_candidate_id or "").strip()
    if not identity or not nexus_id:
        raise ValueError("Nexus identity and candidate id are required")
    timestamp = time.time() if now is None else float(now)
    identity_row = connection.execute(
        "SELECT nexus_candidate_id FROM nexus_candidate_links WHERE identity_key=?",
        (identity,),
    ).fetchone()
    if identity_row and str(identity_row["nexus_candidate_id"]) != nexus_id:
        raise ValueError("local identity is already linked to another Nexus candidate")
    nexus_row = connection.execute(
        "SELECT identity_key FROM nexus_candidate_links WHERE nexus_candidate_id=?",
        (nexus_id,),
    ).fetchone()
    if nexus_row and str(nexus_row["identity_key"]) != identity:
        raise ValueError("Nexus candidate is already linked to another local identity")
    connection.execute(
        """INSERT INTO nexus_candidate_links(
             identity_key,candidate_id,nexus_candidate_id,created,updated
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(identity_key) DO UPDATE SET
             candidate_id=excluded.candidate_id,
             nexus_candidate_id=excluded.nexus_candidate_id,
             updated=excluded.updated""",
        (identity, int(candidate_id), nexus_id, timestamp, timestamp),
    )


def save_nexus_candidate_link(identity_key, candidate_id, nexus_candidate_id):
    with _conn() as connection:
        with connection.transaction():
            _save_nexus_candidate_link(
                connection, identity_key, candidate_id, nexus_candidate_id,
            )
    return get_nexus_candidate_link(identity_key)


def complete_nexus_delivery(
    delivery_id, identity_key, candidate_id, nexus_candidate_id, *, operation="",
    expected_lease_until=None,
):
    """Atomically persist the remote identity link and acknowledge delivery."""
    now = time.time()
    with _conn() as connection:
        with connection.transaction():
            _save_nexus_candidate_link(
                connection, identity_key, candidate_id, nexus_candidate_id, now=now,
            )
            lease_clause = ""
            args = [
                str(nexus_candidate_id or "")[:120],
                str(operation or "delivered")[:80], now, int(delivery_id),
                str(identity_key or "").strip(), int(candidate_id),
            ]
            if expected_lease_until is not None:
                lease_clause = " AND lease_until=?"
                args.append(float(expected_lease_until))
            updated = connection.execute(
                """UPDATE nexus_deliveries
                   SET status='succeeded',next_attempt_at=0,lease_until=0,
                       nexus_candidate_id=?,operation=?,last_error='',updated=?
                   WHERE id=? AND status IN ('processing','writing')
                     AND identity_key=? AND candidate_id=?""" + lease_clause,
                tuple(args),
            )
            if updated.rowcount != 1:
                raise ValueError("Nexus delivery lease is no longer active")


def list_nexus_deliveries(candidate_id=None):
    with _conn() as connection:
        if candidate_id is None:
            rows = connection.execute(
                "SELECT * FROM nexus_deliveries ORDER BY created,id"
            ).fetchall()
        else:
            rows = connection.execute(
                "SELECT * FROM nexus_deliveries WHERE candidate_id=? ORDER BY created,id",
                (int(candidate_id),),
            ).fetchall()
        return [dict(row) for row in rows]


def nexus_delivery_summary(candidate_ids, *, waiting_resume_ids=()):
    """Return the latest durable Nexus outcome for each requested candidate."""
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    waiting_resume_ids = {int(value) for value in waiting_resume_ids or []}
    if not ordered_ids:
        return {
            "selected": 0, "uploaded": 0, "pending": 0,
            "already_in_nexus": 0, "waiting_for_resume": 0,
            "not_uploaded": 0, "items": [],
        }
    placeholders = ",".join("?" for _ in ordered_ids)
    with _conn() as connection:
        candidates = {
            int(row["id"])
            for row in connection.execute(
                f"SELECT id FROM candidates WHERE id IN ({placeholders})", ordered_ids,
            ).fetchall()
        }
        resumes = {
            int(row["candidate_id"]): int(row["resume_count"] or 0)
            for row in connection.execute(
                f"""SELECT candidate_id,COUNT(*) AS resume_count FROM resumes
                    WHERE candidate_id IN ({placeholders}) GROUP BY candidate_id""",
                ordered_ids,
            ).fetchall()
        }
        rows = connection.execute(
            f"""SELECT * FROM nexus_deliveries
                WHERE candidate_id IN ({placeholders}) ORDER BY created,id""",
            ordered_ids,
        ).fetchall()
        check_rows = connection.execute(
            f"""SELECT candidate_id,result FROM nexus_candidate_checks
                WHERE candidate_id IN ({placeholders})""",
            ordered_ids,
        ).fetchall()
    latest = {}
    for row in rows:
        latest[int(row["candidate_id"])] = dict(row)
    blocked = set()
    for row in check_rows:
        try:
            result = json.loads(row["result"] or "{}")
        except (TypeError, ValueError):
            result = {}
        if result.get("blocked") is True and result.get("state") == "active_in_nexus":
            blocked.add(int(row["candidate_id"]))
    pending_states = {"pending", "processing", "writing", "retry"}
    items = []
    for candidate_id in ordered_ids:
        delivery = latest.get(candidate_id)
        status = str((delivery or {}).get("status") or "").casefold()
        if candidate_id in blocked:
            group, reason = "already_in_nexus", "active_in_nexus"
        elif status == "succeeded":
            group, reason = "uploaded", ""
        elif status in pending_states:
            group, reason = "pending", ""
        elif not delivery and candidate_id in waiting_resume_ids:
            group, reason = "waiting_for_resume", "resume_not_saved"
        else:
            group = "not_uploaded"
            if delivery:
                reason = status or "failed"
            elif candidate_id not in candidates:
                reason = "candidate_not_found"
            elif not getattr(config, "NEXUS_SYNC_ENABLED", False):
                reason = "nexus_disabled"
            elif not resumes.get(candidate_id):
                reason = "resume_not_saved"
            else:
                reason = "not_queued"
        items.append({
            "candidate_id": candidate_id,
            "group": group,
            "status": reason if group == "already_in_nexus" else (status or reason),
            "reason": reason,
        })
    return {
        "selected": len(ordered_ids),
        "uploaded": sum(item["group"] == "uploaded" for item in items),
        "pending": sum(item["group"] == "pending" for item in items),
        "already_in_nexus": sum(item["group"] == "already_in_nexus" for item in items),
        "waiting_for_resume": sum(item["group"] == "waiting_for_resume" for item in items),
        "not_uploaded": sum(item["group"] == "not_uploaded" for item in items),
        "enabled": bool(getattr(config, "NEXUS_SYNC_ENABLED", False)),
        "items": items,
    }


def begin_api_request(provider: str, operation: str = "") -> str:
    request_id = f"{time.time_ns():x}-{threading.get_ident():x}"
    now = time.time()
    with _conn() as connection:
        connection.execute(
            """INSERT INTO api_request_activity(
                 request_id,provider,operation,status,started,finished
               ) VALUES(?,?,?,'active',?,0)""",
            (request_id, str(provider)[:80], str(operation)[:120], now),
        )
        connection.execute(
            "DELETE FROM api_request_activity WHERE finished>0 AND finished<?",
            (now - 604800,),
        )
    return request_id


def finish_api_request(request_id: str, status: str = "completed") -> None:
    with _conn() as connection:
        connection.execute(
            """UPDATE api_request_activity SET status=?,finished=?
               WHERE request_id=? AND status='active'""",
            (str(status or "completed")[:40], time.time(), request_id),
        )


def api_request_monitor(provider: str, stale_seconds: float = 240.0) -> dict:
    now = time.time()
    stale_before = now - max(30.0, float(stale_seconds))
    recent_before = now - 300.0
    with _conn() as connection:
        connection.execute(
            """UPDATE api_request_activity SET status='abandoned',finished=?
               WHERE provider=? AND status='active' AND started<?""",
            (now, provider, stale_before),
        )
        row = connection.execute(
            """SELECT
                 SUM(CASE WHEN status='active' THEN 1 ELSE 0 END) AS active,
                 SUM(CASE WHEN started>=? THEN 1 ELSE 0 END) AS started_5m,
                 SUM(CASE WHEN finished>=? AND status='completed' THEN 1 ELSE 0 END) AS completed_5m,
                 SUM(CASE WHEN finished>=? AND status NOT IN ('active','completed') THEN 1 ELSE 0 END) AS failed_5m,
                 MIN(CASE WHEN status='active' THEN started ELSE NULL END) AS oldest_active,
                 AVG(CASE WHEN finished>=? AND finished>started
                          AND status IN ('completed','failed')
                     THEN (finished-started)*1000.0 ELSE NULL END) AS average_response_ms,
                 SUM(CASE WHEN finished>=? AND finished>started
                          AND status IN ('completed','failed') THEN 1 ELSE 0 END) AS measured_responses_5m
               FROM api_request_activity WHERE provider=?""",
            (recent_before, recent_before, recent_before, recent_before, recent_before, provider),
        ).fetchone()
    active = int((row and row["active"]) or 0)
    oldest = float((row and row["oldest_active"]) or 0)
    return {
        "provider": provider,
        "active": active,
        "started_5m": int((row and row["started_5m"]) or 0),
        "completed_5m": int((row and row["completed_5m"]) or 0),
        "failed_5m": int((row and row["failed_5m"]) or 0),
        "average_response_ms_5m": round(float(row["average_response_ms"]), 1)
        if row and row["average_response_ms"] is not None else None,
        "measured_responses_5m": int((row and row["measured_responses_5m"]) or 0),
        "oldest_active_seconds": round(max(0.0, now - oldest), 1) if oldest else 0,
        "measured_at": now,
    }


class ContactLookupQueueLimitError(ValueError):
    def __init__(self, *, limit: int, active: int, requested: int):
        self.limit = int(limit)
        self.active = int(active)
        self.requested = int(requested)
        super().__init__(
            f"You can have at most {self.limit} contact lookups queued or processing at one time."
        )


def contact_lookup_user_limit(
    now: datetime | None = None, limit_override: int | None = None,
) -> int | None:
    """Return the configured cap, lifted after 4 p.m. and on Pacific weekends."""
    pacific_now = (now or datetime.now(ZoneInfo("America/Los_Angeles")))
    if pacific_now.tzinfo is None:
        pacific_now = pacific_now.replace(tzinfo=ZoneInfo("America/Los_Angeles"))
    else:
        pacific_now = pacific_now.astimezone(ZoneInfo("America/Los_Angeles"))
    if pacific_now.weekday() >= 5 or pacific_now.hour >= 16:
        return None
    limit = (
        config.CONTACT_LOOKUP_MAX_OUTSTANDING_PER_USER
        if limit_override is None else int(limit_override)
    )
    return max(1, min(80, int(limit)))


def enqueue_contact_lookup_jobs(run_id: str, candidate_ids, requested_by: str = "",
                                delivery_target: str = "nexus",
                                per_user_limit: int | None = None, *,
                                job_source: str = "extension", priority: int = 100,
                                external_refs: dict[int, str] | None = None,
                                bypass_user_limit: bool = False) -> list[dict]:
    now = time.time()
    normalized_run = str(run_id or "").strip()
    ordered_ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    owner = str(requested_by or "")[:200]
    target = "ceipal" if str(delivery_target or "").casefold() == "ceipal" else "nexus"
    source = "halo_backfill" if str(job_source).casefold() == "halo_backfill" else "extension"
    normalized_priority = max(0, min(1000, int(priority)))
    refs = {int(key): str(value or "")[:200] for key, value in (external_refs or {}).items()}
    limit = None if bypass_user_limit else contact_lookup_user_limit(limit_override=per_user_limit)
    with _CONTACT_LOOKUP_ENQUEUE_LOCK:
        with _conn() as connection:
            if connection.postgres:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))", (f"contact-lookup:{owner}",),
                )
            keys = [f"{owner}:{normalized_run}:{candidate_id}" for candidate_id in ordered_ids]
            existing = set()
            if keys:
                placeholders = ",".join("?" for _ in keys)
                existing = {
                    str(row["job_key"])
                    for row in connection.execute(
                        f"SELECT job_key FROM contact_lookup_queue WHERE job_key IN ({placeholders})",
                        keys,
                    ).fetchall()
                }
            new_jobs = [
                (candidate_id, job_key)
                for candidate_id, job_key in zip(ordered_ids, keys)
                if job_key not in existing
            ]
            active_row = connection.execute(
                """SELECT COUNT(*) AS total FROM contact_lookup_queue
                   WHERE requested_by=? AND status IN ('queued','retry','processing')""",
                (owner,),
            ).fetchone()
            active = int((active_row and active_row["total"]) or 0)
            if limit is not None and active + len(new_jobs) > limit:
                raise ContactLookupQueueLimitError(
                    limit=limit, active=active, requested=len(new_jobs),
                )
            for candidate_id, job_key in new_jobs:
                connection.execute(
                    """INSERT INTO contact_lookup_queue(
                         job_key,run_id,candidate_id,requested_by,delivery_target,
                         job_source,priority,external_ref,status,attempts,
                         next_attempt_at,lease_until,result,last_error,created,updated
                       ) VALUES(?,?,?,?,?,?,?,?,'queued',0,0,0,'{}','',?,?)
                       ON CONFLICT(job_key) DO NOTHING""",
                    (job_key, normalized_run, candidate_id, owner, target, source,
                     normalized_priority, refs.get(candidate_id, ""), now, now),
                )
    return list_contact_lookup_jobs(
        normalized_run, ordered_ids, requested_by=str(requested_by or ""),
    )["items"]


def contact_lookup_paused(requested_by: str) -> bool:
    owner = str(requested_by or "")[:200]
    with _conn() as connection:
        row = connection.execute(
            "SELECT paused FROM contact_lookup_controls WHERE requested_by=?", (owner,),
        ).fetchone()
    return bool(row and row["paused"])


def set_contact_lookup_paused(requested_by: str, paused: bool) -> dict:
    owner = str(requested_by or "")[:200]
    now = time.time()
    with _CONTACT_LOOKUP_CLAIM_LOCK:
        with _conn() as connection:
            if connection.postgres:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))",
                    ("contact-lookup-global-claim",),
                )
            connection.execute(
                """INSERT INTO contact_lookup_controls(requested_by,paused,updated)
                   VALUES(?,?,?)
                   ON CONFLICT(requested_by) DO UPDATE
                   SET paused=excluded.paused,updated=excluded.updated""",
                (owner, 1 if paused else 0, now),
            )
    return {"paused": bool(paused), "updated": now}


def defer_contact_lookup_job(job_id: int, reason: str = "user_paused") -> None:
    with _conn() as connection:
        connection.execute(
            """UPDATE contact_lookup_queue
               SET status='queued',attempts=CASE WHEN attempts>0 THEN attempts-1 ELSE 0 END,
                   next_attempt_at=0,lease_until=0,last_error=?,updated=?
               WHERE id=? AND status='processing'""",
            (str(reason or "")[:1000], time.time(), int(job_id)),
        )


def list_contact_lookup_jobs(run_id: str, candidate_ids=None, requested_by: str = "") -> dict:
    normalized_run = str(run_id or "").strip()
    ids = list(dict.fromkeys(int(value) for value in candidate_ids or []))
    where = "WHERE run_id=?"
    args = [normalized_run]
    if requested_by:
        where += " AND requested_by=?"
        args.append(str(requested_by))
    if ids:
        where += " AND candidate_id IN (" + ",".join("?" for _ in ids) + ")"
        args.extend(ids)
    with _conn() as connection:
        rows = connection.execute(
            f"SELECT * FROM contact_lookup_queue {where} ORDER BY created,id", args,
        ).fetchall()
        active_rows = connection.execute(
            """SELECT id FROM contact_lookup_queue
               WHERE status IN ('queued','retry','processing')
               ORDER BY priority DESC,created,id"""
        ).fetchall()
    positions = {int(row["id"]): index for index, row in enumerate(active_rows)}
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["result"] = json.loads(item.get("result") or "{}")
        except (TypeError, ValueError):
            item["result"] = {}
        item["position"] = positions.get(int(item["id"]), 0)
        items.append(item)
    return {
        "run_id": normalized_run,
        "items": items,
        "queued": sum(item["status"] in {"queued", "retry"} for item in items),
        "processing": sum(item["status"] == "processing" for item in items),
        "complete": sum(item["status"] in {"succeeded", "not_found", "blocked", "failed"} for item in items),
    }


def claim_contact_lookup_jobs(
    limit: int, lease_seconds: float = 210.0,
) -> list[dict]:
    """Claim extension work first and cap the lower-priority Halo backfill."""
    now = time.time()
    lease_until = now + max(30.0, float(lease_seconds))
    concurrency_limit = int(config.CONTACT_LOOKUP_MAX_CONCURRENT)
    requested = max(0, min(concurrency_limit, int(limit or 0)))
    if not requested:
        return []
    with _CONTACT_LOOKUP_CLAIM_LOCK:
        with _conn() as connection, connection.transaction():
            if connection.postgres:
                connection.execute(
                    "SELECT pg_advisory_xact_lock(hashtext(?))",
                    ("contact-lookup-global-claim",),
                )
            connection.execute(
                """UPDATE contact_lookup_queue
                   SET status='retry',lease_until=0,next_attempt_at=?,updated=?,
                       last_error='worker_restarted'
                   WHERE status='processing' AND lease_until<=?""",
                (now, now, now),
            )
            active_count = _scalar(connection.execute(
                """SELECT COUNT(*) FROM contact_lookup_queue
                   WHERE status='processing' AND lease_until>?""", (now,),
            )) or 0
            if int(active_count) >= concurrency_limit:
                return []
            capacity = min(requested, concurrency_limit - int(active_count))
            active_backfill = _scalar(connection.execute(
                """SELECT COUNT(*) FROM contact_lookup_queue
                   WHERE status='processing' AND lease_until>?
                     AND job_source='halo_backfill'""", (now,),
            )) or 0
            common = """q0.status IN ('queued','retry') AND q0.next_attempt_at<=?
                     AND NOT EXISTS (
                       SELECT 1 FROM contact_lookup_controls control
                       WHERE control.requested_by=q0.requested_by AND control.paused=1
                     )"""
            extension_rows = connection.execute(
                f"""SELECT q0.* FROM contact_lookup_queue q0
                   WHERE {common} AND q0.job_source<>'halo_backfill'
                   ORDER BY q0.priority DESC,q0.created,q0.id LIMIT ?""",
                (now, capacity),
            ).fetchall()
            remaining = capacity - len(extension_rows)
            backfill_capacity = min(
                remaining,
                max(0, int(config.CONTACT_LOOKUP_BACKFILL_MAX_CONCURRENT) - int(active_backfill)),
            )
            backfill_rows = connection.execute(
                """SELECT q0.* FROM contact_lookup_queue q0
                   WHERE q0.status IN ('queued','retry') AND q0.next_attempt_at<=?
                     AND q0.job_source='halo_backfill'
                     AND NOT EXISTS (
                       SELECT 1 FROM contact_lookup_controls control
                       WHERE control.requested_by=q0.requested_by AND control.paused=1
                     )
                   ORDER BY q0.priority DESC,q0.created,q0.id LIMIT ?""",
                (now, backfill_capacity),
            ).fetchall() if backfill_capacity else []
            claimed = []
            for row in [*extension_rows, *backfill_rows]:
                updated = connection.execute(
                    """UPDATE contact_lookup_queue
                       SET status='processing',attempts=attempts+1,lease_until=?,updated=?
                       WHERE id=? AND status IN ('queued','retry')""",
                    (lease_until, now, int(row["id"])),
                )
                if updated.rowcount == 1:
                    claimed.append({
                        **dict(row), "status": "processing",
                        "attempts": int(row["attempts"] or 0) + 1,
                        "lease_until": lease_until, "updated": now,
                    })
            return claimed


def claim_contact_lookup_job(lease_seconds: float = 210.0) -> dict | None:
    claimed = claim_contact_lookup_jobs(1, lease_seconds=lease_seconds)
    return claimed[0] if claimed else None


def finish_contact_lookup_job(job_id: int, status: str, result=None, *, error: str = "", retry_at: float = 0) -> None:
    allowed = {"succeeded", "not_found", "blocked", "failed", "retry"}
    normalized = str(status or "").strip().casefold()
    if normalized not in allowed:
        raise ValueError("invalid contact lookup queue status")
    with _conn() as connection:
        connection.execute(
            """UPDATE contact_lookup_queue
               SET status=?,result=?,last_error=?,next_attempt_at=?,
                   lease_until=0,updated=? WHERE id=? AND status='processing'""",
            (
                normalized, json.dumps(result or {}, separators=(",", ":")),
                str(error or "")[:1000], max(0.0, float(retry_at or 0)),
                time.time(), int(job_id),
            ),
        )


def contact_lookup_queue_counts() -> dict:
    now = time.time()
    with _conn() as connection:
        row = connection.execute(
            """SELECT
                 SUM(CASE WHEN status IN ('queued','retry') THEN 1 ELSE 0 END) AS queued,
                 SUM(CASE WHEN status='processing' AND lease_until>? THEN 1 ELSE 0 END) AS processing,
                 SUM(CASE WHEN job_source='halo_backfill' AND status IN ('queued','retry') THEN 1 ELSE 0 END) AS backfill_queued,
                 SUM(CASE WHEN job_source='halo_backfill' AND status='processing' AND lease_until>? THEN 1 ELSE 0 END) AS backfill_processing
               FROM contact_lookup_queue""", (now, now),
        ).fetchone()
        processing_row = connection.execute(
            """SELECT job_key FROM contact_lookup_queue
               WHERE status='processing' AND lease_until>?
                 AND job_source<>'halo_backfill'
               ORDER BY updated,id LIMIT 1""", (now,),
        ).fetchone()
    return {"queued": int((row and row["queued"]) or 0),
            "processing": int((row and row["processing"]) or 0),
            "backfill_queued": int((row and row["backfill_queued"]) or 0),
            "backfill_processing": int((row and row["backfill_processing"]) or 0),
            "processing_job_key": str((processing_row and processing_row["job_key"]) or "")}


# ---- resumes ----
def _save_resume_extraction(
    connection, resume_id, candidate_id, extraction, *, now=None,
):
    if not isinstance(extraction, dict):
        return
    timestamp = float(now if now is not None else time.time())
    serialized = json.dumps(extraction, ensure_ascii=False, separators=(",", ":"))
    connection.execute(
        """INSERT INTO resume_extractions(
             resume_id,candidate_id,extraction,created,updated
           ) VALUES(?,?,?,?,?)
           ON CONFLICT(resume_id) DO UPDATE SET
             candidate_id=excluded.candidate_id,
             extraction=excluded.extraction,
             updated=excluded.updated""",
        (int(resume_id), int(candidate_id), serialized, timestamp, timestamp),
    )


def save_resume_extraction(resume_id, candidate_id, extraction):
    with _conn() as connection:
        _save_resume_extraction(
            connection, resume_id, candidate_id, extraction,
        )
    return get_resume_extraction(resume_id, candidate_id)


def get_resume_extraction(resume_id, candidate_id=None):
    with _conn() as connection:
        if candidate_id is None:
            row = connection.execute(
                "SELECT * FROM resume_extractions WHERE resume_id=?",
                (int(resume_id),),
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM resume_extractions
                   WHERE resume_id=? AND candidate_id=?""",
                (int(resume_id), int(candidate_id)),
            ).fetchone()
    if not row:
        return None
    result = dict(row)
    try:
        result["extraction"] = json.loads(result.get("extraction") or "{}")
    except (TypeError, ValueError):
        result["extraction"] = {}
    return result


def attach_resume(
    candidate_id,
    filename,
    data,
    mime_type="application/pdf",
    *,
    size=None,
    storage_provider="database",
    object_key="",
    bucket="",
    public_url="",
    checksum_sha256="",
    etag="",
    queue_nexus=False,
    requested_by="",
    extraction=None,
):
    if not get_candidate(candidate_id):
        raise ValueError("candidate not found")
    if not isinstance(data, (bytes, bytearray)):
        raise ValueError("resume data must be bytes")
    if storage_provider == "database" and not data:
        raise ValueError("resume data is required")
    if storage_provider != "database" and not object_key:
        raise ValueError("cloud resume object key is required")
    stored_size = int(size if size is not None else len(data))
    checksum = str(checksum_sha256 or "").strip().lower()
    now = time.time()
    nexus_queued = False
    existing = None
    with _conn() as connection:
        with connection.transaction():
            # This upsert is the transaction's first write. It serializes
            # identical captures for one candidate on SQLite and PostgreSQL,
            # closing the prior checksum check-then-insert race.
            connection.execute(
                """INSERT INTO resume_capture_locks(candidate_id,touched)
                   VALUES(?,?) ON CONFLICT(candidate_id) DO UPDATE
                   SET touched=excluded.touched""",
                (int(candidate_id), now),
            )
            if checksum:
                row = connection.execute(
                    """SELECT id,candidate_id,filename,mime_type,size,
                              storage_provider,object_key,bucket,public_url,
                              checksum_sha256,etag,created
                       FROM resumes
                       WHERE candidate_id=? AND LOWER(checksum_sha256)=?
                       ORDER BY created,id LIMIT 1""",
                    (int(candidate_id), checksum),
                ).fetchone()
                existing = dict(row) if row else None
            if existing:
                resume_id = int(existing["id"])
            else:
                resume_id = _insert_id(
                    connection,
                    """INSERT INTO resumes(
                         candidate_id,filename,mime_type,size,data,storage_provider,
                         object_key,bucket,public_url,checksum_sha256,etag,created
                       ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        candidate_id, filename, mime_type, stored_size, bytes(data),
                        storage_provider, object_key, bucket, public_url,
                        checksum, etag, now,
                    ),
                )
            if isinstance(extraction, dict):
                _save_resume_extraction(
                    connection, resume_id, candidate_id, extraction, now=now,
                )
            if queue_nexus and getattr(config, "NEXUS_SYNC_ENABLED", False):
                _enqueue_nexus_delivery(
                    connection, candidate_id, resume_id, checksum, requested_by,
                )
                nexus_queued = True
    delivery = get_nexus_delivery_for_resume(resume_id) if nexus_queued else None
    if existing:
        return {
            **existing,
            "deduplicated": True,
            "nexus_sync_status": delivery.get("status") if delivery else "disabled",
        }
    return {
        "id": resume_id,
        "candidate_id": candidate_id,
        "filename": filename,
        "mime_type": mime_type,
        "size": stored_size,
        "storage_provider": storage_provider,
        "object_key": object_key,
        "bucket": bucket,
        "public_url": public_url,
        "checksum_sha256": checksum,
        "nexus_sync_status": delivery.get("status") if delivery else "disabled",
        "deduplicated": False,
        "created": now,
    }


def list_resumes(candidate_id):
    with _conn() as connection:
        return [
            dict(row)
            for row in connection.execute(
                """SELECT id,candidate_id,filename,mime_type,size,storage_provider,
                          object_key,bucket,public_url,checksum_sha256,etag,created
                   FROM resumes WHERE candidate_id=? ORDER BY created DESC""",
                (candidate_id,),
            )
        ]


def get_resume_by_checksum(candidate_id, checksum_sha256):
    checksum = str(checksum_sha256 or "").strip().lower()
    if not checksum:
        return None
    with _conn() as connection:
        row = connection.execute(
            """SELECT id,candidate_id,filename,mime_type,size,storage_provider,
                      object_key,bucket,public_url,checksum_sha256,etag,created
               FROM resumes
               WHERE candidate_id=? AND LOWER(checksum_sha256)=?
               ORDER BY created DESC LIMIT 1""",
            (candidate_id, checksum),
        ).fetchone()
        return dict(row) if row else None


def get_resume(candidate_id, resume_id):
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM resumes WHERE id=? AND candidate_id=?""",
            (resume_id, candidate_id),
        ).fetchone()
        return dict(row) if row else None


# ---- outreach ----
def save_outreach(candidate_id, channel, subject, body, status="draft"):
    with _conn() as connection:
        return _insert_id(
            connection,
            """INSERT INTO outreach(candidate_id,channel,subject,body,status,created)
               VALUES(?,?,?,?,?,?)""",
            (candidate_id, channel, subject, body, status, time.time()),
        )


def list_outreach(candidate_id):
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM outreach WHERE candidate_id=? ORDER BY created DESC",
            (candidate_id,),
        )]


def mark_outreach(outreach_id, status):
    with _conn() as connection:
        cursor = connection.execute(
            "UPDATE outreach SET status=? WHERE id=?", (status, outreach_id)
        )
        return cursor.rowcount > 0


# ---- SMS conversations and opt-out records ----
def record_sms_consent(candidate_id, phone, status, source, evidence,
                       captured_by="", disclosure_version="v1"):
    normalized_status = str(status or "").strip().casefold()
    if normalized_status not in {"opted_in", "opted_out"}:
        raise ValueError("SMS consent status must be opted_in or opted_out")
    phone_value = str(phone or "").strip()
    key = contact_key(phone_value)
    if not key:
        raise ValueError("A valid phone number is required")
    source_value = str(source or "").strip().casefold()
    if source_value not in {"application", "talent_pool", "inbound_sms", "verbal", "written"}:
        raise ValueError("A documented consent source is required")
    evidence_value = str(evidence or "").strip()
    if not evidence_value:
        raise ValueError("Consent evidence is required")
    now = time.time()
    with _conn() as connection:
        connection.execute(
            """INSERT INTO sms_consents(
                 candidate_id,phone_key,phone,status,source,evidence,
                 disclosure_version,captured_by,captured_at,created,updated
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(candidate_id,phone_key) DO UPDATE SET
                 phone=excluded.phone,status=excluded.status,source=excluded.source,
                 evidence=excluded.evidence,disclosure_version=excluded.disclosure_version,
                 captured_by=excluded.captured_by,captured_at=excluded.captured_at,
                 updated=excluded.updated""",
            (
                int(candidate_id), key, phone_value, normalized_status, source_value,
                evidence_value[:2000], str(disclosure_version or "v1")[:100],
                str(captured_by or "")[:320], now, now, now,
            ),
        )
        if normalized_status == "opted_out":
            connection.execute(
                """INSERT INTO dnc(value,reason,created) VALUES(?,?,?)
                   ON CONFLICT(value) DO NOTHING""",
                (key, "SMS opt-out", now),
            )
        elif source_value == "inbound_sms" and str(captured_by or "") == "zoom":
            # Only an authenticated inbound carrier event may reverse a prior
            # SMS suppression. Manually entered records never clear DNC.
            connection.execute("DELETE FROM dnc WHERE value=?", (key,))
        row = connection.execute(
            "SELECT * FROM sms_consents WHERE candidate_id=? AND phone_key=?",
            (int(candidate_id), key),
        ).fetchone()
        return dict(row)


def get_sms_consent(candidate_id, phone):
    key = contact_key(phone)
    if not key:
        return None
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM sms_consents WHERE candidate_id=? AND phone_key=?",
            (int(candidate_id), key),
        ).fetchone()
        return dict(row) if row else None


def candidate_nexus_id(candidate_id):
    with _conn() as connection:
        row = connection.execute(
            """SELECT nexus_candidate_id FROM nexus_candidate_links
               WHERE candidate_id=? ORDER BY updated DESC LIMIT 1""",
            (int(candidate_id),),
        ).fetchone()
        return str(row["nexus_candidate_id"] or "") if row else ""


def get_or_create_sms_conversation(candidate_id, phone, *, candidate_name="",
                                   initiated_by="", sender_number="", sender_user_id=""):
    phone_value = str(phone or "").strip()
    key = contact_key(phone_value)
    now = time.time()
    with _conn() as connection:
        row = connection.execute(
            """SELECT * FROM sms_conversations
               WHERE candidate_id=? AND phone_key=? AND status NOT IN ('closed','opted_out')
               ORDER BY updated DESC LIMIT 1""",
            (int(candidate_id), key),
        ).fetchone()
        if row:
            return dict(row)
        conversation_id = _insert_id(
            connection,
            """INSERT INTO sms_conversations(
                 candidate_id,nexus_candidate_id,candidate_name,candidate_phone,phone_key,
                 initiated_by,zoom_sender_number,zoom_sender_user_id,status,created,updated,last_message_at
               ) VALUES(?,?,?,?,?,?,?,?,'open',?,?,?)""",
            (
                int(candidate_id), candidate_nexus_id(candidate_id),
                str(candidate_name or "")[:320], phone_value, key,
                str(initiated_by or "")[:320], str(sender_number or "")[:50],
                str(sender_user_id or "")[:80],
                now, now, now,
            ),
        )
        row = connection.execute(
            "SELECT * FROM sms_conversations WHERE id=?", (conversation_id,)
        ).fetchone()
        return dict(row)


def create_sms_message(conversation_id, direction, body, request_id="", status="queued",
                       sender_user_id="", sender_name="", sender_number="", zoom_user_id=""):
    now = time.time()
    request_key = str(request_id or "").strip() or None
    with _conn() as connection:
        if request_key:
            row = connection.execute(
                "SELECT * FROM sms_messages WHERE request_id=?", (request_key,)
            ).fetchone()
            if row:
                return dict(row), False
        message_id = _insert_id(
            connection,
            """INSERT INTO sms_messages(
                 conversation_id,direction,body,status,request_id,sender_user_id,sender_name,
                 sender_number,zoom_user_id,created,updated
               ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                int(conversation_id), str(direction), str(body), str(status),
                request_key, str(sender_user_id or "")[:80], str(sender_name or "")[:160],
                str(sender_number or "")[:50], str(zoom_user_id or "")[:80], now, now,
            ),
        )
        connection.execute(
            "UPDATE sms_conversations SET updated=?,last_message_at=? WHERE id=?",
            (now, now, int(conversation_id)),
        )
        row = connection.execute("SELECT * FROM sms_messages WHERE id=?", (message_id,)).fetchone()
        return dict(row), True


def get_sms_message_by_request_id(request_id):
    request_key = str(request_id or "").strip()
    if not request_key:
        return None
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM sms_messages WHERE request_id=?", (request_key,)
        ).fetchone()
        return dict(row) if row else None


def update_sms_message(message_id, *, status, zoom_message_id="", failure_reason=""):
    now = time.time()
    with _conn() as connection:
        connection.execute(
            """UPDATE sms_messages SET status=?,zoom_message_id=?,failure_reason=?,updated=?
               WHERE id=?""",
            (str(status), str(zoom_message_id or ""), str(failure_reason or "")[:1000], now, int(message_id)),
        )
        row = connection.execute("SELECT * FROM sms_messages WHERE id=?", (int(message_id),)).fetchone()
        return dict(row) if row else None


def update_sms_conversation(conversation_id, **fields):
    allowed = {
        "zoom_session_id", "zoom_sender_number", "zoom_sender_user_id", "status", "assigned_recruiter_id",
        "assigned_recruiter_email", "assigned_recruiter_name",
    }
    values = {key: str(value or "") for key, value in fields.items() if key in allowed}
    if not values:
        return get_sms_conversation(conversation_id)
    values["updated"] = time.time()
    clause = ",".join(f"{key}=?" for key in values)
    with _conn() as connection:
        connection.execute(
            f"UPDATE sms_conversations SET {clause} WHERE id=?",
            (*values.values(), int(conversation_id)),
        )
    return get_sms_conversation(conversation_id)


def get_sms_conversation(conversation_id):
    with _conn() as connection:
        row = connection.execute(
            "SELECT * FROM sms_conversations WHERE id=?", (int(conversation_id),)
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["messages"] = [dict(item) for item in connection.execute(
            "SELECT * FROM sms_messages WHERE conversation_id=? ORDER BY created",
            (int(conversation_id),),
        )]
        return result


def list_sms_conversations(user_id="", *, user_ids=None, include_all=False):
    projection = """SELECT c.*,
        EXISTS(SELECT 1 FROM sms_messages m WHERE m.conversation_id=c.id
               AND m.direction='inbound') AS has_reply,
        (SELECT MAX(m.created) FROM sms_messages m WHERE m.conversation_id=c.id
         AND m.direction='inbound') AS last_reply_at,
        (SELECT MAX(m.created) FROM sms_messages m WHERE m.conversation_id=c.id
         AND m.direction='outbound' AND m.status IN ('accepted','sent','delivered')) AS last_outbound_at,
        (SELECT m.sender_user_id FROM sms_messages m WHERE m.conversation_id=c.id
         AND m.direction='outbound' AND m.status IN ('accepted','sent','delivered')
         ORDER BY m.created DESC LIMIT 1) AS last_outbound_user_id
        FROM sms_conversations c"""
    with _conn() as connection:
        scoped_ids = sorted({str(value or "").strip() for value in (user_ids or [])
                             if str(value or "").strip()})
        if include_all or (user_ids is None and not user_id):
            rows = connection.execute(
                projection + " ORDER BY c.updated DESC"
            ).fetchall()
        elif user_ids is not None:
            if not scoped_ids:
                return []
            placeholders = ",".join("?" for _ in scoped_ids)
            rows = connection.execute(
                projection + f" WHERE c.initiated_by IN ({placeholders})"
                f" OR c.assigned_recruiter_id IN ({placeholders})"
                " ORDER BY c.updated DESC",
                (*scoped_ids, *scoped_ids),
            ).fetchall()
        else:
            rows = connection.execute(
                projection + " WHERE c.initiated_by=? OR c.assigned_recruiter_id=?"
                " ORDER BY c.updated DESC",
                (str(user_id), str(user_id)),
            ).fetchall()
        return [dict(row) for row in rows]


def claim_sms_webhook(event_key, event_type):
    with _conn() as connection:
        cursor = connection.execute(
            """INSERT INTO sms_webhook_events(event_key,event_type,created)
               VALUES(?,?,?) ON CONFLICT(event_key) DO NOTHING""",
            (str(event_key), str(event_type), time.time()),
        )
        return cursor.rowcount > 0


def find_sms_conversation(*, session_id="", phone=""):
    with _conn() as connection:
        if session_id:
            row = connection.execute(
                """SELECT * FROM sms_conversations WHERE zoom_session_id=?
                   ORDER BY updated DESC LIMIT 1""", (str(session_id),)
            ).fetchone()
        else:
            row = connection.execute(
                """SELECT * FROM sms_conversations WHERE phone_key=?
                   ORDER BY updated DESC LIMIT 1""", (contact_key(phone),)
            ).fetchone()
        return dict(row) if row else None


def sms_candidate_contacted(candidate_id, phone="") -> bool:
    """Whether any outbound SMS already exists for this candidate/number."""
    params = [int(candidate_id)]
    phone_clause = ""
    if phone:
        phone_clause = " AND c.phone_key=?"
        params.append(contact_key(phone))
    with _conn() as connection:
        row = connection.execute(
            """SELECT 1 FROM sms_messages m
               JOIN sms_conversations c ON c.id=m.conversation_id
               WHERE c.candidate_id=? AND m.direction='outbound' AND m.status<>'failed'""" + phone_clause +
            " LIMIT 1",
            tuple(params),
        ).fetchone()
        return bool(row)


def claim_sms_outreach(candidate_id, phone):
    """Atomically reserve first outbound SMS for a candidate across workers."""
    now = time.time()
    with _conn() as connection:
        cursor = connection.execute(
            """INSERT INTO sms_outreach_claims(candidate_id,phone_key,status,created,updated)
               VALUES(?,?,'sending',?,?)
               ON CONFLICT(candidate_id) DO UPDATE SET
                 phone_key=excluded.phone_key,status='sending',updated=excluded.updated
               WHERE sms_outreach_claims.status='failed'""",
            (int(candidate_id), contact_key(phone), now, now),
        )
        return cursor.rowcount > 0


def update_sms_outreach_claim(candidate_id, status):
    with _conn() as connection:
        connection.execute(
            "UPDATE sms_outreach_claims SET status=?,updated=? WHERE candidate_id=?",
            (str(status), time.time(), int(candidate_id)),
        )


def reconcile_outbound_sms(conversation_id, *, zoom_message_id="", status="sent",
                           failure_reason=""):
    now = time.time()
    with _conn() as connection:
        row = None
        if zoom_message_id:
            row = connection.execute(
                "SELECT * FROM sms_messages WHERE zoom_message_id=? LIMIT 1",
                (str(zoom_message_id),),
            ).fetchone()
        if not row:
            row = connection.execute(
                """SELECT * FROM sms_messages
                   WHERE conversation_id=? AND direction='outbound'
                   ORDER BY created DESC LIMIT 1""",
                (int(conversation_id),),
            ).fetchone()
        if not row:
            return None
        connection.execute(
            """UPDATE sms_messages SET status=?,zoom_message_id=?,failure_reason=?,updated=?
               WHERE id=?""",
            (
                str(status), str(zoom_message_id or row["zoom_message_id"] or ""),
                str(failure_reason or "")[:1000], now, int(row["id"]),
            ),
        )
        result = connection.execute(
            "SELECT * FROM sms_messages WHERE id=?", (int(row["id"]),)
        ).fetchone()
        return dict(result)


# ---- watcher email delivery claims ----
def claim_watcher_email_delivery(event_id, recipient, lease_seconds=60):
    """Claim one event/recipient send while preserving SendGrid idempotency.

    A completed claim is never sent again. Failed and expired in-flight claims
    may be retried by a later watcher cycle.
    """
    normalized_event = str(event_id or "").strip()
    normalized_recipient = str(recipient or "").strip().casefold()
    if not normalized_event or not normalized_recipient:
        raise ValueError("event_id and recipient are required")
    now = time.time()
    lease_until = now + max(10.0, float(lease_seconds or 60))
    with _conn() as connection:
        with connection.transaction():
            connection.execute(
                """INSERT INTO watcher_email_deliveries(
                     event_id,recipient,status,attempts,lease_until,last_error,
                     created,updated
                   ) VALUES(?,?,'pending',0,0,'',?,?)
                   ON CONFLICT(event_id,recipient) DO NOTHING""",
                (normalized_event, normalized_recipient, now, now),
            )
            row = connection.execute(
                """SELECT * FROM watcher_email_deliveries
                   WHERE event_id=? AND recipient=?""",
                (normalized_event, normalized_recipient),
            ).fetchone()
            delivery = dict(row) if row else None
            if not delivery:
                return {"claimed": False, "status": "missing"}
            status = str(delivery.get("status") or "pending")
            if status == "sent":
                return {**delivery, "claimed": False}
            if status == "sending" and float(delivery.get("lease_until") or 0) > now:
                return {**delivery, "claimed": False}
            connection.execute(
                """UPDATE watcher_email_deliveries
                   SET status='sending', attempts=attempts+1, lease_until=?,
                       last_error='', updated=?
                   WHERE event_id=? AND recipient=?""",
                (lease_until, now, normalized_event, normalized_recipient),
            )
            row = connection.execute(
                """SELECT * FROM watcher_email_deliveries
                   WHERE event_id=? AND recipient=?""",
                (normalized_event, normalized_recipient),
            ).fetchone()
            return {**dict(row), "claimed": True}


def finish_watcher_email_delivery(event_id, recipient, *, sent, error=""):
    status = "sent" if sent else "failed"
    now = time.time()
    with _conn() as connection:
        cursor = connection.execute(
            """UPDATE watcher_email_deliveries
               SET status=?, lease_until=0, last_error=?, updated=?
               WHERE event_id=? AND recipient=?""",
            (
                status,
                str(error or "").replace("\x00", "")[:1000],
                now,
                str(event_id or "").strip(),
                str(recipient or "").strip().casefold(),
            ),
        )
        return cursor.rowcount > 0


def list_watcher_email_deliveries(event_id=""):
    with _conn() as connection:
        if event_id:
            rows = connection.execute(
                """SELECT * FROM watcher_email_deliveries
                   WHERE event_id=? ORDER BY recipient""",
                (str(event_id).strip(),),
            )
        else:
            rows = connection.execute(
                "SELECT * FROM watcher_email_deliveries ORDER BY created DESC"
            )
        return [dict(row) for row in rows]


# ---- do-not-contact ----
def contact_key(value) -> str:
    """Normalize one suppression value so phone formatting cannot bypass DNC."""
    cleaned = str(value or "").strip().casefold()
    if not cleaned:
        return ""
    if "@" in cleaned:
        return cleaned
    digits = "".join(character for character in cleaned if character.isdigit())
    if len(digits) >= 7:
        return digits[-10:] if len(digits) >= 10 else digits
    return cleaned


def _blocked_contact_keys(connection) -> set[str]:
    return {
        key for key in (
            contact_key(row["value"])
            for row in connection.execute("SELECT value FROM dnc")
        ) if key
    }


def dnc_contact_keys() -> set[str]:
    """Load the suppression set once for a batch contact projection."""
    with _conn() as connection:
        return _blocked_contact_keys(connection)


def add_dnc(value, reason=""):
    key = contact_key(value)
    if not key:
        return
    with _conn() as connection:
        connection.execute(
            """INSERT INTO dnc(value,reason,created) VALUES(?,?,?)
               ON CONFLICT(value) DO NOTHING""",
            (key, reason, time.time()),
        )


def is_dnc(value):
    key = contact_key(value)
    if not key:
        return False
    with _conn() as connection:
        return key in _blocked_contact_keys(connection)


def filter_dnc(values):
    """Return non-suppressed values using one database round trip."""
    cleaned = [str(value or "").strip() for value in values or []]
    cleaned = [value for value in cleaned if value]
    if not cleaned:
        return []
    normalized = list(dict.fromkeys(contact_key(value) for value in cleaned))
    with _conn() as connection:
        blocked = _blocked_contact_keys(connection)
    return [value for value in cleaned if contact_key(value) not in blocked]


def dnc_blocked(values):
    """Return the suppressed values, lowercased, using one database query.

    Batch enrichment resolves the whole run's contacts here once and then
    filters in memory instead of querying per candidate.
    """
    normalized = list(dict.fromkeys(
        contact_key(cleaned)
        for cleaned in (str(value or "").strip() for value in values or [])
        if cleaned
    ))
    if not normalized:
        return set()
    with _conn() as connection:
        blocked = _blocked_contact_keys(connection)
    return set(normalized) & blocked


def filter_dnc_groups(groups, *, blocked_keys=None):
    """Filter contact groups, optionally using a preloaded suppression set."""
    cleaned_groups = {}
    all_normalized = []
    for group, values in (groups or {}).items():
        cleaned = [str(value or "").strip() for value in values or []]
        cleaned = [value for value in cleaned if value]
        cleaned_groups[group] = cleaned
        all_normalized.extend(contact_key(value) for value in cleaned)
    normalized = list(dict.fromkeys(all_normalized))
    if not normalized:
        return cleaned_groups
    if blocked_keys is None:
        with _conn() as connection:
            blocked = _blocked_contact_keys(connection)
    else:
        blocked = set(blocked_keys)
    return {
        group: [value for value in values if contact_key(value) not in blocked]
        for group, values in cleaned_groups.items()
    }


def list_dnc():
    with _conn() as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM dnc ORDER BY created DESC"
        )]


def stats():
    with _conn() as connection:
        total = _scalar(connection.execute("SELECT COUNT(*) FROM candidates"))
        by_stage = {stage: 0 for stage in PIPELINE_STAGES}
        for row in connection.execute(
            "SELECT stage,COUNT(*) AS count FROM candidates GROUP BY stage"
        ):
            by_stage[row["stage"]] = row["count"]
        enriched = _scalar(connection.execute(
            "SELECT COUNT(*) FROM candidates WHERE enrich_status='success'"
        ))
        jobs = _scalar(connection.execute("SELECT COUNT(*) FROM jobs"))
        dnc = _scalar(connection.execute("SELECT COUNT(*) FROM dnc"))
        return {
            "total_candidates": total,
            "by_stage": by_stage,
            "enriched": enriched,
            "jobs": jobs,
            "dnc": dnc,
            "database": backend_name(),
        }


def upsert_user(auth0_sub: str, *, email: str = "", name: str = "") -> dict:
    """Create/update the local analytics identity for an external user ID."""
    subject = str(auth0_sub or "").strip()[:255]
    if not subject:
        raise ValueError("auth0_sub is required")
    now = time.time()
    with _conn() as connection:
        with connection.transaction():
            connection.execute(
                """INSERT INTO users(auth0_sub,email,name,created,updated)
                   VALUES(?,?,?,?,?)
                   ON CONFLICT(auth0_sub) DO UPDATE SET
                     email=excluded.email,name=excluded.name,updated=excluded.updated""",
                (subject, str(email or "")[:320], str(name or "")[:320], now, now),
            )
            row = connection.execute(
                "SELECT id,auth0_sub,email,name,created,updated FROM users WHERE auth0_sub=?",
                (subject,),
            ).fetchone()
    return dict(row)


def record_enrichment_event(auth0_sub: str, candidate_id: int, status: str,
                            *, provider: str = "", run_id: str = "") -> None:
    """Attribute one enrichment attempt to the authenticated user."""
    subject = str(auth0_sub or "local").strip()[:255] or "local"
    with _conn() as connection:
        connection.execute(
            """INSERT INTO enrichment_events(
                 auth0_sub,candidate_id,status,provider,run_id,created
               ) VALUES(?,?,?,?,?,?)""",
            (subject, int(candidate_id), str(status or "unknown")[:80],
             str(provider or "")[:80], str(run_id or "")[:120], time.time()),
        )


def user_enrichment_stats(auth0_sub: str) -> dict:
    subject = str(auth0_sub or "").strip()[:255]
    with _conn() as connection:
        attempts = _scalar(connection.execute(
            "SELECT COUNT(*) FROM enrichment_events WHERE auth0_sub=?", (subject,)
        ))
        successful = _scalar(connection.execute(
            """SELECT COUNT(*) FROM enrichment_events
               WHERE auth0_sub=? AND status IN ('success','found')""", (subject,)
        ))
        candidates = _scalar(connection.execute(
            """SELECT COUNT(DISTINCT candidate_id) FROM enrichment_events
               WHERE auth0_sub=? AND status IN ('success','found')""", (subject,)
        ))
    return {
        "auth0_sub": subject,
        "enrichment_attempts": int(attempts or 0),
        "successful_enrichments": int(successful or 0),
        "candidates_enriched": int(candidates or 0),
    }


def all_user_enrichment_stats() -> list[dict]:
    """Return aggregate enrichment attribution for the analytics permission."""
    with _conn() as connection:
        rows = connection.execute(
            """SELECT u.auth0_sub,u.email,u.name,
                      COUNT(e.id) AS enrichment_attempts,
                      COALESCE(SUM(CASE WHEN e.status IN ('success','found') THEN 1 ELSE 0 END),0) AS successful_enrichments,
                      COUNT(DISTINCT CASE WHEN e.status IN ('success','found') THEN e.candidate_id END) AS candidates_enriched
               FROM users u LEFT JOIN enrichment_events e ON e.auth0_sub=u.auth0_sub
               GROUP BY u.auth0_sub,u.email,u.name ORDER BY u.name,u.email"""
        ).fetchall()
    return [dict(row) for row in rows]


def reset():
    """Wipe the SQLite test/demo database. Production PostgreSQL reset is blocked."""
    if config.DATABASE_URL:
        raise RuntimeError("Refusing to reset a configured PostgreSQL database.")
    path = Path(config.DB_PATH)
    if path.exists():
        path.unlink()
