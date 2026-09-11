from __future__ import annotations

import json
import os
import queue
import threading
import time
from contextlib import nullcontext
from datetime import datetime
from typing import Any

from hosted_facilitator.hosted.control_plane import (
    ControlPlaneAuditEvent,
    ControlPlanePauseState,
    ControlPlaneTargetType,
    _normalize_pause_target,
    _safe_actor,
    _safe_audit_reason,
    _safe_correlation_id,
    _safe_key_prefix,
    _safe_network_or_none,
    _safe_operator_lease_owner,
    _safe_provider_name,
    _safe_reason,
    _safe_reconciliation_action,
    _safe_seller_key_action,
    _safe_seller_ref,
    _seller_key_audit_reason,
)
from hosted_facilitator.hosted.limits import DEFAULT_RATE_LIMIT_POLICY, RateLimitPolicy
from hosted_facilitator.hosted.migrations import (
    HostedPostgresMigration,
    apply_hosted_postgres_migrations,
    hosted_postgres_migrations_current,
)
from hosted_facilitator.hosted.storage import (
    SettlementAttemptRecord,
    SettlementAttemptStatus,
    SettlementClaim,
    SettlementRecord,
    SettlementStatus,
    _claim_for_status,
    _parse_datetime,
    _safe_requirements,
    utcnow,
)
from hosted_facilitator.hosted.tenancy import (
    IssuedSellerKey,
    PaymentProfileConfig,
    ResolvedSellerAccess,
    SellerAccountConfig,
    _bearer_token,
    issue_seller_api_key,
    seller_key_hash,
    seller_key_prefix,
)

POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settlement_records (
    id BIGSERIAL PRIMARY KEY,
    project_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL,
    scheme TEXT,
    network TEXT,
    status TEXT NOT NULL,
    trace_id TEXT,
    transaction_hash TEXT,
    payer TEXT,
    error_reason TEXT,
    reconciliation_owner TEXT,
    reconciliation_lease_until TIMESTAMPTZ,
    reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
    raw_requirements_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(project_id, fingerprint)
);

ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS reconciliation_owner TEXT;
ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS reconciliation_lease_until TIMESTAMPTZ;
ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS reconciliation_attempts INTEGER NOT NULL DEFAULT 0;

CREATE INDEX IF NOT EXISTS idx_settlement_records_status_updated
    ON settlement_records(status, updated_at);
CREATE INDEX IF NOT EXISTS idx_settlement_records_trace_id
    ON settlement_records(trace_id);
CREATE INDEX IF NOT EXISTS idx_settlement_records_provider_network_status
    ON settlement_records(provider, network, status);
CREATE INDEX IF NOT EXISTS idx_settlement_records_reconciliation_lease
    ON settlement_records(status, updated_at, reconciliation_lease_until);

CREATE TABLE IF NOT EXISTS settlement_attempts (
    id BIGSERIAL PRIMARY KEY,
    settlement_record_id BIGINT NOT NULL REFERENCES settlement_records(id),
    trace_id TEXT,
    status TEXT NOT NULL,
    started_at TIMESTAMPTZ NOT NULL,
    finished_at TIMESTAMPTZ,
    transaction_hash TEXT,
    error_reason TEXT
);

CREATE INDEX IF NOT EXISTS idx_settlement_attempts_record_started
    ON settlement_attempts(settlement_record_id, started_at);
"""

POSTGRES_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS settlement_records (
    id BIGSERIAL PRIMARY KEY,
    seller_account_id TEXT NOT NULL,
    payment_profile_id TEXT NOT NULL DEFAULT 'default',
    fingerprint TEXT NOT NULL,
    provider TEXT NOT NULL,
    scheme TEXT,
    network TEXT,
    status TEXT NOT NULL,
    trace_id TEXT,
    transaction_hash TEXT,
    payer TEXT,
    error_reason TEXT,
    reconciliation_owner TEXT,
    reconciliation_lease_until TIMESTAMPTZ,
    reconciliation_attempts INTEGER NOT NULL DEFAULT 0,
    duplicate_claims INTEGER NOT NULL DEFAULT 0,
    last_duplicate_at TIMESTAMPTZ,
    raw_requirements_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    UNIQUE(seller_account_id, payment_profile_id, fingerprint)
);
"""

POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL = """
ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS duplicate_claims INTEGER NOT NULL DEFAULT 0;
ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS last_duplicate_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_settlement_records_last_duplicate_at
    ON settlement_records(last_duplicate_at);
"""

POSTGRES_SETTLEMENT_SELLER_ACCOUNTS_SQL = """
DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'settlement_records'
          AND column_name = 'project_id'
    ) AND EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'settlement_records'
          AND column_name = 'seller_account_id'
    ) THEN
        IF EXISTS (
            SELECT 1
            FROM settlement_records
            WHERE seller_account_id IS NOT NULL
              AND seller_account_id <> project_id
        ) THEN
            RAISE EXCEPTION
                'Ambiguous hosted settlement ownership: project_id and seller_account_id differ';
        END IF;
    END IF;

    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'settlement_records'
          AND column_name = 'project_id'
    ) AND NOT EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'settlement_records'
          AND column_name = 'seller_account_id'
    ) THEN
        ALTER TABLE settlement_records
            RENAME COLUMN project_id TO seller_account_id;
    END IF;
END $$;

ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS seller_account_id TEXT;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1
        FROM information_schema.columns
        WHERE table_schema = current_schema()
          AND table_name = 'settlement_records'
          AND column_name = 'project_id'
    ) THEN
        UPDATE settlement_records
        SET seller_account_id = project_id
        WHERE seller_account_id IS NULL;
    END IF;
END $$;

ALTER TABLE IF EXISTS settlement_records
    ALTER COLUMN seller_account_id SET NOT NULL;

ALTER TABLE IF EXISTS settlement_records
    DROP CONSTRAINT IF EXISTS settlement_records_project_id_fingerprint_key;
DROP INDEX IF EXISTS uniq_settlement_records_provider_fingerprint;

DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT constraint_record.conname
        FROM pg_constraint constraint_record
        JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
        JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
        WHERE namespace_record.nspname = current_schema()
          AND table_record.relname = 'settlement_records'
          AND constraint_record.contype = 'u'
          AND (
            SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
            FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
            JOIN pg_attribute attribute_record
              ON attribute_record.attrelid = table_record.oid
             AND attribute_record.attnum = key_record.attnum
          ) = ARRAY['project_id', 'fingerprint']
    LOOP
        EXECUTE format('ALTER TABLE settlement_records DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

"""

POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL = """
ALTER TABLE IF EXISTS settlement_records
    ADD COLUMN IF NOT EXISTS payment_profile_id TEXT NOT NULL DEFAULT 'default';

ALTER TABLE IF EXISTS settlement_records
    DROP CONSTRAINT IF EXISTS settlement_records_seller_account_id_fingerprint_key;

DO $$
DECLARE
    constraint_name text;
BEGIN
    FOR constraint_name IN
        SELECT constraint_record.conname
        FROM pg_constraint constraint_record
        JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
        JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
        WHERE namespace_record.nspname = current_schema()
          AND table_record.relname = 'settlement_records'
          AND constraint_record.contype = 'u'
          AND (
            SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
            FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
            JOIN pg_attribute attribute_record
              ON attribute_record.attrelid = table_record.oid
             AND attribute_record.attnum = key_record.attnum
          ) = ARRAY['seller_account_id', 'fingerprint']
    LOOP
        EXECUTE format('ALTER TABLE settlement_records DROP CONSTRAINT %I', constraint_name);
    END LOOP;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint constraint_record
        JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
        JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
        WHERE namespace_record.nspname = current_schema()
          AND table_record.relname = 'settlement_records'
          AND constraint_record.contype = 'u'
          AND (
            SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
            FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
            JOIN pg_attribute attribute_record
              ON attribute_record.attrelid = table_record.oid
             AND attribute_record.attnum = key_record.attnum
          ) = ARRAY['seller_account_id', 'payment_profile_id', 'fingerprint']
    ) THEN
        ALTER TABLE settlement_records
            ADD CONSTRAINT settlement_records_seller_profile_fingerprint_key
            UNIQUE (seller_account_id, payment_profile_id, fingerprint);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_settlement_records_seller_profile
    ON settlement_records(seller_account_id, payment_profile_id);

CREATE INDEX IF NOT EXISTS idx_settlement_records_provider_fingerprint_lookup
    ON settlement_records(
        seller_account_id,
        provider,
        COALESCE(scheme, ''),
        COALESCE(network, ''),
        fingerprint
    );
"""

POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL = """
DROP INDEX IF EXISTS uniq_settlement_records_provider_fingerprint;
DROP INDEX IF EXISTS idx_settlement_records_provider_fingerprint_lookup;

CREATE INDEX idx_settlement_records_provider_fingerprint_lookup
    ON settlement_records(
        seller_account_id,
        provider,
        COALESCE(scheme, ''),
        COALESCE(network, ''),
        fingerprint
    );
"""

POSTGRES_SETTLEMENT_SELLER_STATUS_UPDATED_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_settlement_records_seller_status_updated
    ON settlement_records(seller_account_id, status, updated_at);
"""

POSTGRES_CONTROL_PLANE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS control_plane_pause_targets (
    target_type TEXT NOT NULL,
    target TEXT NOT NULL,
    paused BOOLEAN NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(target_type, target)
);

CREATE INDEX IF NOT EXISTS idx_control_plane_pause_targets_paused
    ON control_plane_pause_targets(target_type, target)
    WHERE paused = TRUE;

CREATE TABLE IF NOT EXISTS control_plane_audit_events (
    id BIGSERIAL PRIMARY KEY,
    action TEXT NOT NULL,
    target_type TEXT NOT NULL,
    target TEXT NOT NULL,
    before_paused BOOLEAN NOT NULL,
    after_paused BOOLEAN NOT NULL,
    reason TEXT NOT NULL,
    actor TEXT NOT NULL,
    correlation_id TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_control_plane_audit_events_created
    ON control_plane_audit_events(created_at DESC, id DESC);
"""

POSTGRES_CONTROL_PLANE_SELLER_AUDIT_TARGETS_SQL = """
UPDATE control_plane_audit_events
SET target_type = 'seller'
WHERE target_type = 'project';
"""

POSTGRES_CONTROL_PLANE_TARGET_TYPE_CONSTRAINT_SQL = """
DELETE FROM control_plane_audit_events
WHERE target_type NOT IN ('global', 'provider', 'network', 'seller');

DO $$
BEGIN
    ALTER TABLE control_plane_audit_events
        ADD CONSTRAINT control_plane_audit_events_target_type_check
        CHECK (target_type IN ('global', 'provider', 'network', 'seller'));
EXCEPTION
    WHEN duplicate_object THEN NULL;
END $$;
"""

POSTGRES_CONTROL_PLANE_SETTLEMENT_AUDIT_TARGET_SQL = """
DO $$
BEGIN
    ALTER TABLE control_plane_audit_events
        DROP CONSTRAINT IF EXISTS control_plane_audit_events_target_type_check;
    ALTER TABLE control_plane_audit_events
        ADD CONSTRAINT control_plane_audit_events_target_type_check
        CHECK (target_type IN ('global', 'provider', 'network', 'seller', 'settlement'));
END $$;
"""

POSTGRES_SELLER_ACCOUNTS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS hosted_tenants (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS hosted_seller_accounts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES hosted_tenants(id),
    name TEXT,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hosted_seller_accounts_tenant_status
    ON hosted_seller_accounts(tenant_id, status);

CREATE TABLE IF NOT EXISTS hosted_seller_payment_profiles (
    id TEXT NOT NULL,
    seller_account_id TEXT NOT NULL REFERENCES hosted_seller_accounts(id),
    name TEXT,
    status TEXT NOT NULL,
    enabled_networks_json JSONB NOT NULL,
    enabled_schemes_json JSONB NOT NULL,
    enabled_providers_json JSONB NOT NULL,
    allowed_assets_json JSONB NOT NULL,
    allowed_pay_to_json JSONB NOT NULL,
    rate_limits_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(seller_account_id, id)
);

CREATE INDEX IF NOT EXISTS idx_hosted_seller_payment_profiles_seller_status
    ON hosted_seller_payment_profiles(seller_account_id, status);

CREATE TABLE IF NOT EXISTS hosted_seller_api_keys (
    key_hash TEXT PRIMARY KEY,
    key_id TEXT NOT NULL UNIQUE,
    seller_account_id TEXT NOT NULL,
    payment_profile_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY(seller_account_id, payment_profile_id)
        REFERENCES hosted_seller_payment_profiles(seller_account_id, id)
);

CREATE INDEX IF NOT EXISTS idx_hosted_seller_api_keys_seller_status
    ON hosted_seller_api_keys(seller_account_id, status);
CREATE INDEX IF NOT EXISTS idx_hosted_seller_api_keys_profile_status
    ON hosted_seller_api_keys(seller_account_id, payment_profile_id, status);
"""

POSTGRES_SELLER_PROJECT_COMPATIBILITY_SQL = """
DO $$
BEGIN
    IF to_regclass(format('%I.hosted_projects', current_schema())) IS NOT NULL THEN
        INSERT INTO hosted_tenants (id, name, status, created_at, updated_at)
        SELECT tenant_id, tenant_id, 'active', MIN(created_at), MAX(updated_at)
        FROM hosted_projects
        GROUP BY tenant_id
        ON CONFLICT (id) DO NOTHING;

        INSERT INTO hosted_seller_accounts (
            id, tenant_id, name, environment, status, created_at, updated_at
        )
        SELECT id, tenant_id, name, environment, status, created_at, updated_at
        FROM hosted_projects
        ON CONFLICT (id) DO NOTHING;

        INSERT INTO hosted_seller_payment_profiles (
            id, seller_account_id, name, status, enabled_networks_json,
            enabled_schemes_json, enabled_providers_json, allowed_assets_json,
            allowed_pay_to_json, rate_limits_json, created_at, updated_at
        )
        SELECT 'default', id, COALESCE(name, id), status, enabled_networks_json,
            enabled_schemes_json, enabled_providers_json, allowed_assets_json,
            allowed_pay_to_json,
            CASE
                WHEN rate_limits_json ? 'sellerRequestsPerMinute' THEN rate_limits_json
                WHEN rate_limits_json ? 'projectRequestsPerMinute' THEN
                    jsonb_set(
                        rate_limits_json - 'projectRequestsPerMinute',
                        '{sellerRequestsPerMinute}',
                        rate_limits_json->'projectRequestsPerMinute'
                    )
                ELSE rate_limits_json
            END,
            created_at, updated_at
        FROM hosted_projects
        ON CONFLICT (seller_account_id, id) DO NOTHING;
    END IF;

END $$;
"""

POSTGRES_SELLER_REVOKE_PROJECT_API_KEYS_SQL = """
DELETE FROM hosted_seller_api_keys
WHERE key_id LIKE 'migrated_%';
"""

POSTGRES_SELLER_PAYMENT_PROFILES_SQL = """
CREATE TABLE IF NOT EXISTS hosted_tenants (
    id TEXT PRIMARY KEY,
    name TEXT,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE TABLE IF NOT EXISTS hosted_seller_accounts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL REFERENCES hosted_tenants(id),
    name TEXT,
    environment TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_hosted_seller_accounts_tenant_status
    ON hosted_seller_accounts(tenant_id, status);

CREATE TABLE IF NOT EXISTS hosted_seller_payment_profiles (
    id TEXT NOT NULL,
    seller_account_id TEXT NOT NULL REFERENCES hosted_seller_accounts(id),
    name TEXT,
    status TEXT NOT NULL,
    enabled_networks_json JSONB NOT NULL,
    enabled_schemes_json JSONB NOT NULL,
    enabled_providers_json JSONB NOT NULL,
    allowed_assets_json JSONB NOT NULL,
    allowed_pay_to_json JSONB NOT NULL,
    rate_limits_json JSONB NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL,
    PRIMARY KEY(seller_account_id, id)
);

CREATE INDEX IF NOT EXISTS idx_hosted_seller_payment_profiles_seller_status
    ON hosted_seller_payment_profiles(seller_account_id, status);

CREATE TABLE IF NOT EXISTS hosted_seller_api_keys (
    key_hash TEXT PRIMARY KEY,
    key_id TEXT NOT NULL UNIQUE,
    seller_account_id TEXT NOT NULL,
    payment_profile_id TEXT NOT NULL,
    key_prefix TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL,
    revoked_at TIMESTAMPTZ,
    FOREIGN KEY(seller_account_id, payment_profile_id)
        REFERENCES hosted_seller_payment_profiles(seller_account_id, id)
);

ALTER TABLE IF EXISTS hosted_seller_api_keys
    ADD COLUMN IF NOT EXISTS key_id TEXT;
ALTER TABLE IF EXISTS hosted_seller_api_keys
    ADD COLUMN IF NOT EXISTS payment_profile_id TEXT NOT NULL DEFAULT 'default';

CREATE INDEX IF NOT EXISTS idx_hosted_seller_api_keys_seller_status
    ON hosted_seller_api_keys(seller_account_id, status);
CREATE INDEX IF NOT EXISTS idx_hosted_seller_api_keys_profile_status
    ON hosted_seller_api_keys(seller_account_id, payment_profile_id, status);
"""

POSTGRES_SETTLEMENT_MIGRATIONS = (
    HostedPostgresMigration(
        scope="settlement",
        version="202605170001",
        description="create hosted settlement records and attempts",
        sql=POSTGRES_LEGACY_PROJECT_SETTLEMENT_SCHEMA_SQL,
        accepted_checksums=(
            "aa4b4719822ff7e9b96799a14e9a6c1d2e28e48ec419ca62b275497efd8b1375",
            "aa4b47eb4fd06d49263843f6b786a76bc95abf7faf68f1c97bce7b33b277f73f",
        ),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200001",
        description="add hosted settlement duplicate metrics",
        sql=POSTGRES_SETTLEMENT_DUPLICATE_METRICS_SQL,
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="2026052000011",
        description="rename hosted settlement records from projects to seller accounts",
        sql=POSTGRES_SETTLEMENT_SELLER_ACCOUNTS_SQL,
        accepted_checksums=("45c4bb9f5b633879433164721ab09c08cdc1e0b8f1c0675f7fc3ee92b5ba8318",),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200002",
        description="scope hosted settlement records to payment profiles",
        sql=POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL,
        accepted_checksums=(
            "f8f85195efb18a12b045c9e8b0fa94280cdec5274e28e91d1078ef56dce092a9",
            "931eac5c3821a3df41cbe9e9eae107d3b87fd5fc7a464179ac622c1779b0ebe7",
            "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
            "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
        ),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200003",
        description="re-assert hosted settlement payment profile schema",
        sql=POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL,
        accepted_checksums=(
            "f8f85195efb18a12b045c9e8b0fa94280cdec5274e28e91d1078ef56dce092a9",
            "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
            "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
        ),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200004",
        description="re-assert hosted settlement final constraints and lookup index",
        sql=POSTGRES_SETTLEMENT_PAYMENT_PROFILES_SQL,
        accepted_checksums=(
            "2b1739edeae15a6b8a26f30ba0b28eb756fe567a411e0a039b0d926fa5acd8d0",
            "592f694abc7760edad64dbc8211443872417cadd1bc070f945122e0c4e48ce34",
        ),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200005",
        description="drop stale global provider fingerprint index",
        sql=POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL,
        accepted_checksums=("23a62acc1523d854ab1f6725aa725d2d13a5be1be61a8c9bb6cc8fad4f7bf77a",),
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605200006",
        description="scope provider fingerprint replay to sellers",
        sql=POSTGRES_SETTLEMENT_PROVIDER_FINGERPRINT_LOOKUP_SQL,
    ),
    HostedPostgresMigration(
        scope="settlement",
        version="202605280002",
        description="index seller scoped settlement health queries",
        sql=POSTGRES_SETTLEMENT_SELLER_STATUS_UPDATED_INDEX_SQL,
    ),
)

POSTGRES_CONTROL_PLANE_MIGRATIONS = (
    HostedPostgresMigration(
        scope="control_plane",
        version="202605170001",
        description="create hosted control-plane pause and audit tables",
        sql=POSTGRES_CONTROL_PLANE_SCHEMA_SQL,
    ),
    HostedPostgresMigration(
        scope="control_plane",
        version="202605200001",
        description="rename hosted control-plane project audit targets to sellers",
        sql=POSTGRES_CONTROL_PLANE_SELLER_AUDIT_TARGETS_SQL,
    ),
    HostedPostgresMigration(
        scope="control_plane",
        version="202605200002",
        description="constrain hosted control-plane audit target types",
        sql=POSTGRES_CONTROL_PLANE_TARGET_TYPE_CONSTRAINT_SQL,
    ),
    HostedPostgresMigration(
        scope="control_plane",
        version="202605280001",
        description="allow hosted control-plane settlement audit targets",
        sql=POSTGRES_CONTROL_PLANE_SETTLEMENT_AUDIT_TARGET_SQL,
    ),
)

POSTGRES_SELLER_ACCOUNTS_MIGRATIONS = (
    HostedPostgresMigration(
        scope="seller_accounts",
        version="202605170001",
        description="create hosted tenants, seller accounts, and seller API keys",
        sql=POSTGRES_SELLER_ACCOUNTS_SCHEMA_SQL,
        accepted_checksums=("66a51f11deff8111ac18a037f21374f230e35b3f99dfc623e5428a59cecfc285",),
    ),
    HostedPostgresMigration(
        scope="seller_accounts",
        version="202605200001",
        description="create hosted seller payment profiles",
        sql=POSTGRES_SELLER_PAYMENT_PROFILES_SQL,
        accepted_checksums=("33b2531c827a71a0ed4ebd02edcdb7ba7f20a58e6d3c9c75389d2f81d4342e36",),
    ),
    HostedPostgresMigration(
        scope="seller_accounts",
        version="202605200002",
        description="copy project-scoped sellers into seller accounts",
        sql=POSTGRES_SELLER_PROJECT_COMPATIBILITY_SQL,
        accepted_checksums=(
            "0952f75c69778db7f0922d4a5d77f2e3e9e05696dc7bb98dda5ab6321f83c4e7",
            "5610611582a076fe05e2850de3abc248c1850e3356d06f957fc6ea8539aeca22",
        ),
    ),
    HostedPostgresMigration(
        scope="seller_accounts",
        version="202605200003",
        description="remove project-imported hosted seller API keys",
        sql=POSTGRES_SELLER_REVOKE_PROJECT_API_KEYS_SQL,
    ),
)

POSTGRES_SELLER_ACCOUNTS_HEALTH_SQL = """
SELECT
  to_regclass(format('%I.hosted_tenants', current_schema())) IS NOT NULL
    AS hosted_tenants_table,
  to_regclass(format('%I.hosted_seller_accounts', current_schema())) IS NOT NULL
    AS hosted_seller_accounts_table,
  to_regclass(format('%I.hosted_seller_payment_profiles', current_schema())) IS NOT NULL
    AS hosted_seller_payment_profiles_table,
  to_regclass(format('%I.hosted_seller_api_keys', current_schema())) IS NOT NULL
    AS hosted_seller_api_keys_table,
  to_regclass(format('%I.idx_hosted_seller_accounts_tenant_status', current_schema())) IS NOT NULL
    AS hosted_seller_accounts_tenant_status_index,
  to_regclass(format('%I.idx_hosted_seller_api_keys_seller_status', current_schema())) IS NOT NULL
    AS hosted_seller_api_keys_seller_status_index,
  to_regclass(format('%I.idx_hosted_seller_payment_profiles_seller_status', current_schema())) IS NOT NULL
    AS hosted_seller_payment_profiles_seller_status_index,
  to_regclass(format('%I.idx_hosted_seller_api_keys_profile_status', current_schema())) IS NOT NULL
    AS hosted_seller_api_keys_profile_status_index,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_tenants'
      AND constraint_record.contype = 'p'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['id']
  ) AS hosted_tenants_primary_key,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_accounts'
      AND constraint_record.contype = 'p'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['id']
  ) AS hosted_seller_accounts_primary_key,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_accounts'
      AND constraint_record.contype = 'f'
      AND constraint_record.confrelid = (
        SELECT referenced_table.oid
        FROM pg_class referenced_table
        JOIN pg_namespace referenced_namespace ON referenced_namespace.oid = referenced_table.relnamespace
        WHERE referenced_namespace.nspname = current_schema()
          AND referenced_table.relname = 'hosted_tenants'
      )
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['tenant_id']
  ) AS hosted_seller_accounts_tenant_foreign_key,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_api_keys'
      AND constraint_record.contype = 'p'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['key_hash']
  ) AS api_key_hash_primary_key,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_payment_profiles'
      AND constraint_record.contype = 'p'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['seller_account_id', 'id']
  ) AS hosted_seller_payment_profiles_primary_key,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_api_keys'
      AND constraint_record.contype = 'u'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['key_id']
  ) AS api_key_id_unique,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'hosted_seller_api_keys'
      AND constraint_record.contype = 'f'
      AND constraint_record.confrelid = (
        SELECT referenced_table.oid
        FROM pg_class referenced_table
        JOIN pg_namespace referenced_namespace ON referenced_namespace.oid = referenced_table.relnamespace
        WHERE referenced_namespace.nspname = current_schema()
          AND referenced_table.relname = 'hosted_seller_payment_profiles'
      )
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['seller_account_id', 'payment_profile_id']
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.confkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_class referenced_table ON referenced_table.oid = constraint_record.confrelid
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = referenced_table.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['seller_account_id', 'id']
  ) AS api_keys_payment_profile_foreign_key
"""

CLAIM_RECORD_SQL = """
WITH inserted AS (
    INSERT INTO settlement_records (
        seller_account_id, payment_profile_id, fingerprint, provider, scheme, network,
        status, trace_id, raw_requirements_json, created_at, updated_at
    )
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
    ON CONFLICT DO NOTHING
    RETURNING *, TRUE AS inserted
)
SELECT * FROM inserted
UNION ALL
SELECT settlement_records.*, FALSE AS inserted
FROM settlement_records
WHERE seller_account_id = %s AND payment_profile_id = %s AND fingerprint = %s
LIMIT 1
"""

INSERT_ATTEMPT_SQL = """
INSERT INTO settlement_attempts (
    settlement_record_id, trace_id, status, started_at
)
VALUES (%s, %s, %s, %s)
RETURNING id
"""

LIST_RECONCILIATION_CANDIDATES_SQL = """
SELECT *
FROM settlement_records
WHERE status = ANY(%s)
  AND updated_at <= %s
  AND (
      reconciliation_lease_until IS NULL
      OR reconciliation_lease_until <= %s
  )
ORDER BY updated_at ASC, id ASC
LIMIT %s
"""

CLAIM_RECONCILIATION_RECORD_SQL = """
UPDATE settlement_records
SET reconciliation_owner = %s,
    reconciliation_lease_until = %s,
    reconciliation_attempts = reconciliation_attempts + 1
WHERE id = %s
  AND status = ANY(%s)
  AND (%s::timestamptz IS NULL OR updated_at <= %s::timestamptz)
  AND (
      reconciliation_lease_until IS NULL
      OR reconciliation_lease_until <= %s
  )
RETURNING *
"""

LIST_STALE_STARTED_ATTEMPTS_SQL = """
SELECT *
FROM settlement_attempts
WHERE status = %s
  AND started_at <= %s
ORDER BY started_at ASC, id ASC
LIMIT %s
"""

PROVIDER_FINGERPRINT_LOCK_SQL = "SELECT pg_advisory_xact_lock(hashtextextended(%s, 0))"

POSTGRES_HEALTH_SQL = """
SELECT
  to_regclass(format('%I.settlement_records', current_schema())) IS NOT NULL
    AS settlement_records_table,
  to_regclass(format('%I.settlement_attempts', current_schema())) IS NOT NULL
    AS settlement_attempts_table,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'settlement_records'
      AND constraint_record.contype = 'u'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['seller_account_id', 'payment_profile_id', 'fingerprint']
  ) AS seller_profile_fingerprint_unique,
  NOT EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'settlement_records'
      AND constraint_record.contype = 'u'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['seller_account_id', 'fingerprint']
  ) AS no_stale_seller_fingerprint_unique,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_status_updated'
  ) AS status_updated_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_provider_network_status'
  ) AS provider_network_status_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_reconciliation_lease'
  ) AS reconciliation_lease_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_attempts'
      AND indexname = 'idx_settlement_attempts_record_started'
  ) AS attempts_record_started_index,
  EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'settlement_records'
      AND column_name = 'duplicate_claims'
  ) AS duplicate_claims_column,
  EXISTS (
    SELECT 1
    FROM information_schema.columns
    WHERE table_schema = current_schema()
      AND table_name = 'settlement_records'
      AND column_name = 'last_duplicate_at'
  ) AS last_duplicate_at_column,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_last_duplicate_at'
  ) AS last_duplicate_at_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_provider_fingerprint_lookup'
  ) AS provider_fingerprint_lookup_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'settlement_records'
      AND indexname = 'idx_settlement_records_seller_status_updated'
  ) AS seller_status_updated_index
"""

POSTGRES_CONTROL_PLANE_HEALTH_SQL = """
SELECT
  to_regclass(format('%I.control_plane_pause_targets', current_schema())) IS NOT NULL
    AS control_plane_pause_targets_table,
  to_regclass(format('%I.control_plane_audit_events', current_schema())) IS NOT NULL
    AS control_plane_audit_events_table,
  EXISTS (
    SELECT 1
    FROM pg_constraint constraint_record
    JOIN pg_class table_record ON table_record.oid = constraint_record.conrelid
    JOIN pg_namespace namespace_record ON namespace_record.oid = table_record.relnamespace
    WHERE namespace_record.nspname = current_schema()
      AND table_record.relname = 'control_plane_pause_targets'
      AND constraint_record.contype = 'p'
      AND (
        SELECT array_agg(attribute_record.attname::text ORDER BY key_record.ordinality)
        FROM unnest(constraint_record.conkey) WITH ORDINALITY AS key_record(attnum, ordinality)
        JOIN pg_attribute attribute_record
          ON attribute_record.attrelid = table_record.oid
         AND attribute_record.attnum = key_record.attnum
      ) = ARRAY['target_type', 'target']
  ) AS pause_targets_primary_key,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'control_plane_pause_targets'
      AND indexname = 'idx_control_plane_pause_targets_paused'
  ) AS pause_targets_paused_index,
  EXISTS (
    SELECT 1
    FROM pg_indexes
    WHERE schemaname = current_schema()
      AND tablename = 'control_plane_audit_events'
      AND indexname = 'idx_control_plane_audit_events_created'
  ) AS audit_events_created_index
"""

_CLAIM_VISIBILITY_RETRIES = 3
_CLAIM_VISIBILITY_RETRY_SECONDS = 0.01


class PostgresControlPlaneState:
    """Postgres-backed hosted control-plane pause and audit state."""

    durable = True
    writes_enabled = True

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        hosted_safe: bool | None = None,
    ):
        if connection is None:
            if not dsn:
                raise ValueError("PostgresControlPlaneState requires a DSN or connection")
            connection = _connect(dsn)
            self.hosted_safe = True if hosted_safe is None else hosted_safe
        else:
            self.hosted_safe = False if hosted_safe is None else hosted_safe
        self._conn = connection

    def initialize(self) -> None:
        apply_hosted_postgres_migrations(
            self._conn,
            scope="control_plane",
            migrations=POSTGRES_CONTROL_PLANE_MIGRATIONS,
            validate_schema=self._schema_health_check,
        )

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()

    def health_check(self) -> bool:
        return self._schema_health_check() and hosted_postgres_migrations_current(
            self._conn,
            scope="control_plane",
            migrations=POSTGRES_CONTROL_PLANE_MIGRATIONS,
        )

    def _schema_health_check(self) -> bool:
        row = self._fetchone(POSTGRES_CONTROL_PLANE_HEALTH_SQL)
        if row is None:
            return False
        return all(
            bool(row.get(key))
            for key in (
                "control_plane_pause_targets_table",
                "control_plane_audit_events_table",
                "pause_targets_primary_key",
                "pause_targets_paused_index",
                "audit_events_created_index",
            )
        )

    def pause_state(self) -> ControlPlanePauseState:
        rows = self._fetchall(
            """
            SELECT target_type, target
            FROM control_plane_pause_targets
            WHERE paused = TRUE
            """
        )
        state = ControlPlanePauseState()
        for row in rows:
            target_type = ControlPlaneTargetType(row["target_type"])
            target = str(row["target"])
            if target_type == ControlPlaneTargetType.GLOBAL:
                state.global_paused = True
            elif target_type == ControlPlaneTargetType.PROVIDER:
                state.providers.add(_safe_provider_name(target))
            elif target_type == ControlPlaneTargetType.NETWORK:
                safe_network = _safe_network_or_none(target)
                if safe_network is not None:
                    state.networks.add(safe_network)
        return state

    def audit_tail(self, limit: int = 25) -> list[ControlPlaneAuditEvent]:
        rows = self._fetchall(
            """
            SELECT *
            FROM control_plane_audit_events
            ORDER BY id DESC
            LIMIT %s
            """,
            (max(0, min(limit, 100)),),
        )
        events: list[ControlPlaneAuditEvent] = []
        for row in rows:
            try:
                events.append(_control_plane_audit_event_from_mapping(row))
            except ValueError:
                continue
        return events

    def allows(self, *, provider: str, network: str | None) -> bool:
        targets = [
            (ControlPlaneTargetType.GLOBAL.value, "global"),
            (ControlPlaneTargetType.PROVIDER.value, _safe_provider_name(provider)),
        ]
        safe_network = _safe_network_or_none(network)
        if safe_network is not None:
            targets.append((ControlPlaneTargetType.NETWORK.value, safe_network))
        clauses = " OR ".join(["(target_type = %s AND target = %s)" for _ in targets])
        params = tuple(value for pair in targets for value in pair)
        row = self._fetchone(
            f"""
            SELECT TRUE AS blocked
            FROM control_plane_pause_targets
            WHERE paused = TRUE
              AND ({clauses})
            LIMIT 1
            """,
            params,
        )
        return row is None

    def set_pause(
        self,
        *,
        target_type: ControlPlaneTargetType,
        target: str | None,
        paused: bool,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        normalized_target = _normalize_pause_target(target_type=target_type, target=target)
        now = utcnow()
        with self._transaction():
            self._execute(
                """
                INSERT INTO control_plane_pause_targets (
                    target_type, target, paused, updated_at
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (target_type, target) DO NOTHING
                """,
                (target_type.value, normalized_target, False, now),
            )
            locked = self._fetchone(
                """
                SELECT paused
                FROM control_plane_pause_targets
                WHERE target_type = %s AND target = %s
                FOR UPDATE
                """,
                (target_type.value, normalized_target),
            )
            if locked is None:
                raise RuntimeError("Postgres control-plane pause target was not locked")
            before = bool(locked.get("paused"))
            self._execute(
                """
                INSERT INTO control_plane_pause_targets (
                    target_type, target, paused, updated_at
                )
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (target_type, target)
                DO UPDATE SET paused = EXCLUDED.paused, updated_at = EXCLUDED.updated_at
                """,
                (target_type.value, normalized_target, bool(paused), now),
            )
            row = self._fetchone(
                """
                INSERT INTO control_plane_audit_events (
                    action, target_type, target, before_paused, after_paused,
                    reason, actor, correlation_id, created_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING *
                """,
                (
                    "pause_set",
                    target_type.value,
                    normalized_target,
                    before,
                    bool(paused),
                    _safe_reason(reason),
                    _safe_actor(actor),
                    _safe_correlation_id(correlation_id),
                    now,
                ),
            )
            if row is None:
                raise RuntimeError("Postgres control-plane audit event was not created")
            return _control_plane_audit_event_from_mapping(row)

    def record_seller_create(
        self,
        *,
        seller_account_id: str,
        key_prefix: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        now = utcnow()
        row = self._fetchone(
            """
            INSERT INTO control_plane_audit_events (
                action, target_type, target, before_paused, after_paused,
                reason, actor, correlation_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                "seller_create",
                ControlPlaneTargetType.SELLER.value,
                _safe_seller_ref(seller_account_id),
                False,
                True,
                f"api_key_prefix:{_safe_key_prefix(key_prefix)}",
                _safe_actor(actor),
                _safe_correlation_id(correlation_id),
                now,
            ),
        )
        if row is None:
            raise RuntimeError("Postgres control-plane seller account audit event was not created")
        return _control_plane_audit_event_from_mapping(row)

    def record_seller_key_event(
        self,
        *,
        action: str,
        seller_account_id: str,
        key_prefix: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        safe_action = _safe_seller_key_action(action)
        now = utcnow()
        row = self._fetchone(
            """
            INSERT INTO control_plane_audit_events (
                action, target_type, target, before_paused, after_paused,
                reason, actor, correlation_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                safe_action,
                ControlPlaneTargetType.SELLER.value,
                _safe_seller_ref(seller_account_id),
                safe_action == "seller_api_key_revoke",
                safe_action == "seller_api_key_issue",
                f"api_key_prefix:{_safe_key_prefix(key_prefix)}",
                _safe_actor(actor),
                _safe_correlation_id(correlation_id),
                now,
            ),
        )
        if row is None:
            raise RuntimeError("Postgres control-plane seller API-key audit event was not created")
        return _control_plane_audit_event_from_mapping(row)

    def record_reconciliation_event(
        self,
        *,
        action: str,
        record_id: int,
        before: bool,
        after: bool,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> ControlPlaneAuditEvent:
        safe_action = _safe_reconciliation_action(action)
        safe_record_id = int(record_id)
        if safe_record_id <= 0:
            raise ValueError("settlement record id is required")
        now = utcnow()
        row = self._fetchone(
            """
            INSERT INTO control_plane_audit_events (
                action, target_type, target, before_paused, after_paused,
                reason, actor, correlation_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                safe_action,
                ControlPlaneTargetType.SETTLEMENT.value,
                str(safe_record_id),
                bool(before),
                bool(after),
                _safe_reason(reason),
                _safe_actor(actor),
                _safe_correlation_id(correlation_id),
                now,
            ),
        )
        if row is None:
            raise RuntimeError("Postgres control-plane reconciliation audit event was not created")
        return _control_plane_audit_event_from_mapping(row)

    def _is_paused(self, *, target_type: ControlPlaneTargetType, target: str) -> bool:
        row = self._fetchone(
            """
            SELECT paused
            FROM control_plane_pause_targets
            WHERE target_type = %s AND target = %s
            """,
            (target_type.value, target),
        )
        return bool(row and row.get("paused"))

    def _transaction(self):
        transaction = getattr(self._conn, "transaction", None)
        if transaction is None:
            return nullcontext()
        return transaction()

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        return self._conn.execute(sql, params)

    def _commit(self) -> None:
        commit = getattr(self._conn, "commit", None)
        if commit is not None:
            commit()

    def _fetchone(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        result = self._execute(sql, params)
        if result is None:
            return None
        row = result.fetchone()
        return dict(row) if row is not None and not isinstance(row, dict) else row

    def _fetchall(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        result = self._execute(sql, params)
        if result is None:
            return []
        rows = result.fetchall()
        return [dict(row) if not isinstance(row, dict) else row for row in rows]


class PostgresSellerAccountResolver:
    """Postgres-backed hosted seller account resolver and key registry."""

    durable = True
    hosted_safe = True

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        hosted_safe: bool | None = None,
        default_seller_account_id: str | None = None,
    ):
        if connection is None:
            if not dsn:
                raise ValueError("PostgresSellerAccountResolver requires a DSN or connection")
            connection = _connect(dsn)
            self.hosted_safe = True if hosted_safe is None else hosted_safe
        else:
            self.hosted_safe = False if hosted_safe is None else hosted_safe
        self._conn = connection
        self._default_seller_account_id = default_seller_account_id

    def initialize(self) -> None:
        apply_hosted_postgres_migrations(
            self._conn,
            scope="seller_accounts",
            migrations=POSTGRES_SELLER_ACCOUNTS_MIGRATIONS,
            validate_schema=self._schema_health_check,
        )

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()

    def health_check(self) -> bool:
        return self._schema_health_check() and hosted_postgres_migrations_current(
            self._conn,
            scope="seller_accounts",
            migrations=POSTGRES_SELLER_ACCOUNTS_MIGRATIONS,
        )

    def _schema_health_check(self) -> bool:
        row = self._fetchone(POSTGRES_SELLER_ACCOUNTS_HEALTH_SQL)
        if row is None:
            return False
        return all(
            bool(row.get(key))
            for key in (
                "hosted_tenants_table",
                "hosted_seller_accounts_table",
                "hosted_seller_payment_profiles_table",
                "hosted_seller_api_keys_table",
                "hosted_seller_accounts_tenant_status_index",
                "hosted_seller_payment_profiles_seller_status_index",
                "hosted_seller_api_keys_seller_status_index",
                "hosted_seller_api_keys_profile_status_index",
                "hosted_tenants_primary_key",
                "hosted_seller_accounts_primary_key",
                "hosted_seller_accounts_tenant_foreign_key",
                "hosted_seller_payment_profiles_primary_key",
                "api_key_hash_primary_key",
                "api_key_id_unique",
                "api_keys_payment_profile_foreign_key",
            )
        )

    def resolve(
        self,
        *,
        authorization: str | None = None,
        require_auth: bool = False,
    ) -> SellerAccountConfig:
        return self.resolve_access(
            authorization=authorization, require_auth=require_auth
        ).seller_account

    def resolve_access(
        self,
        *,
        authorization: str | None = None,
        require_auth: bool = False,
    ) -> ResolvedSellerAccess:
        token = _bearer_token(authorization)
        if token:
            return self._resolve_api_key(token)
        if self._default_seller_account_id is not None and not require_auth:
            seller_account = self.get_seller_account(self._default_seller_account_id)
            if seller_account is not None:
                return ResolvedSellerAccess(
                    seller_account=seller_account,
                    payment_profile=seller_account.default_payment_profile(),
                    api_key_prefix=None,
                )
        if require_auth:
            raise PermissionError("Seller API-key authentication is required")
        raise PermissionError("Seller API-key authentication is required")

    def create_seller_account(
        self,
        *,
        seller_account_id: str,
        tenant_id: str,
        name: str | None = None,
        environment: str = "testnet",
        enabled_networks: tuple[str, ...] = ("eip155:5042002",),
        enabled_schemes: tuple[str, ...] = ("exact",),
        enabled_providers: tuple[str, ...] = ("exact_evm",),
        allowed_assets: tuple[str, ...] = (),
        allowed_pay_to: tuple[str, ...] = (),
        rate_limits: RateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY,
    ) -> SellerAccountConfig:
        now = utcnow()
        with self._transaction():
            self._execute(
                """
                INSERT INTO hosted_tenants (id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET updated_at = EXCLUDED.updated_at
                """,
                (tenant_id, tenant_id, "active", now, now),
            )
            tenant = self._fetchone(
                """
                SELECT status
                FROM hosted_tenants
                WHERE id = %s
                FOR UPDATE
                """,
                (tenant_id,),
            )
            if tenant is None or str(tenant.get("status")) != "active":
                raise ValueError("Tenant is not active")
            self._execute(
                """
                INSERT INTO hosted_seller_accounts (
                    id, tenant_id, name, environment, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    seller_account_id,
                    tenant_id,
                    name or seller_account_id,
                    environment,
                    "active",
                    now,
                    now,
                ),
            )
            self._upsert_payment_profile(
                payment_profile=PaymentProfileConfig(
                    payment_profile_id="default",
                    seller_account_id=seller_account_id,
                    name=name or seller_account_id,
                    status="active",
                    enabled_networks=enabled_networks,
                    enabled_schemes=enabled_schemes,
                    enabled_providers=enabled_providers,
                    allowed_assets=tuple(value.lower() for value in allowed_assets),
                    allowed_pay_to=tuple(value.lower() for value in allowed_pay_to),
                    rate_limits=rate_limits,
                ),
                now=now,
            )
        seller_account = self.get_seller_account(seller_account_id)
        if seller_account is None:
            raise RuntimeError("Postgres seller account was not created")
        return seller_account

    def create_seller_account_with_api_key(
        self,
        *,
        seller_account_id: str,
        tenant_id: str,
        name: str | None = None,
        environment: str = "testnet",
        enabled_networks: tuple[str, ...] = ("eip155:5042002",),
        enabled_schemes: tuple[str, ...] = ("exact",),
        enabled_providers: tuple[str, ...] = ("exact_evm",),
        allowed_assets: tuple[str, ...] = (),
        allowed_pay_to: tuple[str, ...] = (),
        rate_limits: RateLimitPolicy = DEFAULT_RATE_LIMIT_POLICY,
        audit_actor: str | None = None,
        audit_correlation_id: str | None = None,
    ) -> (
        tuple[SellerAccountConfig, IssuedSellerKey]
        | tuple[SellerAccountConfig, IssuedSellerKey, ControlPlaneAuditEvent]
    ):
        now = utcnow()
        issued = issue_seller_api_key(seller_account_id, payment_profile_id="default")
        audit_event: ControlPlaneAuditEvent | None = None
        with self._transaction():
            self._execute(
                """
                INSERT INTO hosted_tenants (id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET updated_at = EXCLUDED.updated_at
                """,
                (tenant_id, tenant_id, "active", now, now),
            )
            tenant = self._fetchone(
                """
                SELECT status
                FROM hosted_tenants
                WHERE id = %s
                FOR UPDATE
                """,
                (tenant_id,),
            )
            if tenant is None or str(tenant.get("status")) != "active":
                raise ValueError("Tenant is not active")
            inserted = self._fetchone(
                """
                INSERT INTO hosted_seller_accounts (
                    id, tenant_id, name, environment, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                RETURNING id
                """,
                (
                    seller_account_id,
                    tenant_id,
                    name or seller_account_id,
                    environment,
                    "active",
                    now,
                    now,
                ),
            )
            if inserted is None:
                raise ValueError("Seller account already exists")
            self._upsert_payment_profile(
                payment_profile=PaymentProfileConfig(
                    payment_profile_id="default",
                    seller_account_id=seller_account_id,
                    name=name or seller_account_id,
                    status="active",
                    enabled_networks=enabled_networks,
                    enabled_schemes=enabled_schemes,
                    enabled_providers=enabled_providers,
                    allowed_assets=tuple(value.lower() for value in allowed_assets),
                    allowed_pay_to=tuple(value.lower() for value in allowed_pay_to),
                    rate_limits=rate_limits,
                ),
                now=now,
            )
            self._execute(
                """
                INSERT INTO hosted_seller_api_keys (
                    key_hash, key_id, seller_account_id, payment_profile_id, key_prefix,
                    status, created_at, revoked_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
                """,
                (
                    issued.key_hash,
                    issued.key_id,
                    seller_account_id,
                    issued.payment_profile_id,
                    issued.key_prefix,
                    "active",
                    now,
                ),
            )
            if audit_actor is not None and audit_correlation_id is not None:
                audit_event = self._insert_seller_audit_event(
                    action="seller_create",
                    seller_account_id=seller_account_id,
                    key_prefix_value=issued.key_prefix,
                    actor=audit_actor,
                    correlation_id=audit_correlation_id,
                    now=now,
                )
        seller_account = self.get_seller_account(seller_account_id)
        if seller_account is None:
            raise RuntimeError("Postgres seller account was not created")
        if audit_event is not None:
            return seller_account, issued, audit_event
        return seller_account, issued

    def bootstrap_seller_accounts(self, seller_accounts: list[SellerAccountConfig]) -> None:
        for seller_account in seller_accounts:
            self.bootstrap_seller_account(seller_account)

    def bootstrap_seller_account(self, seller_account: SellerAccountConfig) -> None:
        now = utcnow()
        with self._transaction():
            self._execute(
                """
                INSERT INTO hosted_tenants (id, name, status, created_at, updated_at)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (seller_account.tenant_id, seller_account.tenant_id, "active", now, now),
            )
            tenant = self._fetchone(
                """
                SELECT status
                FROM hosted_tenants
                WHERE id = %s
                FOR UPDATE
                """,
                (seller_account.tenant_id,),
            )
            if tenant is None or str(tenant.get("status")) != "active":
                raise ValueError("Tenant is not active")
            self._execute(
                """
                INSERT INTO hosted_seller_accounts (
                    id, tenant_id, name, environment, status, created_at, updated_at
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE
                SET name = EXCLUDED.name,
                    environment = EXCLUDED.environment,
                    updated_at = EXCLUDED.updated_at
                WHERE hosted_seller_accounts.status = 'active'
                """,
                (
                    seller_account.seller_account_id,
                    seller_account.tenant_id,
                    seller_account.seller_account_id,
                    seller_account.environment,
                    "active",
                    now,
                    now,
                ),
            )
            profiles = seller_account.payment_profiles or (
                seller_account.default_payment_profile(),
            )
            for profile in profiles:
                self._upsert_payment_profile(payment_profile=profile, now=now)
            for api_key in seller_account.api_keys:
                self._upsert_bootstrap_api_key(
                    api_key=api_key,
                    seller_account_id=seller_account.seller_account_id,
                    payment_profile_id=seller_account.default_payment_profile().payment_profile_id,
                    now=now,
                )
            for profile in profiles:
                for api_key in profile.api_keys:
                    self._upsert_bootstrap_api_key(
                        api_key=api_key,
                        seller_account_id=seller_account.seller_account_id,
                        payment_profile_id=profile.payment_profile_id,
                        now=now,
                    )
        if self.get_seller_account(seller_account.seller_account_id) is None:
            raise RuntimeError("Postgres bootstrap seller_account was not created")

    def issue_api_key(
        self, seller_account_id: str, payment_profile_id: str = "default"
    ) -> IssuedSellerKey:
        issued = issue_seller_api_key(seller_account_id, payment_profile_id=payment_profile_id)
        self._insert_key(
            table="hosted_seller_api_keys",
            hash_column="key_hash",
            hash_value=issued.key_hash,
            key_id=issued.key_id,
            seller_account_id=seller_account_id,
            payment_profile_id=payment_profile_id,
            key_prefix_value=issued.key_prefix,
        )
        return issued

    def issue_api_key_with_audit(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        actor: str,
        correlation_id: str,
    ) -> tuple[IssuedSellerKey, ControlPlaneAuditEvent]:
        issued = issue_seller_api_key(seller_account_id, payment_profile_id=payment_profile_id)
        now = utcnow()
        with self._transaction():
            self._assert_active_payment_profile(
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
            )
            self._insert_seller_api_key_row(
                key_hash=issued.key_hash,
                key_id=issued.key_id,
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
                key_prefix_value=issued.key_prefix,
                now=now,
            )
            audit_event = self._insert_seller_audit_event(
                action="seller_api_key_issue",
                seller_account_id=seller_account_id,
                key_prefix_value=issued.key_prefix,
                actor=actor,
                correlation_id=correlation_id,
                now=now,
            )
        return issued, audit_event

    def list_seller_accounts(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT
                hosted_seller_accounts.id,
                hosted_seller_accounts.tenant_id,
                hosted_seller_accounts.name,
                hosted_seller_accounts.environment,
                hosted_seller_accounts.status,
                hosted_seller_accounts.created_at,
                hosted_seller_accounts.updated_at,
                COUNT(DISTINCT hosted_seller_payment_profiles.id) AS payment_profile_count,
                COUNT(DISTINCT hosted_seller_api_keys.key_id)
                    FILTER (
                        WHERE hosted_seller_api_keys.status = 'active'
                          AND hosted_seller_api_keys.revoked_at IS NULL
                    ) AS active_api_key_count
            FROM hosted_seller_accounts
            JOIN hosted_tenants ON hosted_tenants.id = hosted_seller_accounts.tenant_id
            LEFT JOIN hosted_seller_payment_profiles
              ON hosted_seller_payment_profiles.seller_account_id = hosted_seller_accounts.id
            LEFT JOIN hosted_seller_api_keys
              ON hosted_seller_api_keys.seller_account_id = hosted_seller_accounts.id
            WHERE hosted_tenants.status = 'active'
            GROUP BY hosted_seller_accounts.id
            ORDER BY hosted_seller_accounts.created_at DESC, hosted_seller_accounts.id ASC
            LIMIT %s
            """,
            (max(0, min(int(limit), 500)),),
        )
        return [_seller_admin_summary_from_mapping(row) for row in rows]

    def list_seller_api_keys(self, seller_account_id: str) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT key_id, seller_account_id, payment_profile_id, key_prefix,
                   status, created_at, revoked_at
            FROM hosted_seller_api_keys
            WHERE seller_account_id = %s
            ORDER BY created_at DESC, key_id ASC
            """,
            (seller_account_id,),
        )
        return [_seller_api_key_summary_from_mapping(row) for row in rows]

    def revoke_api_key(self, *, seller_account_id: str, key_id: str) -> dict[str, Any]:
        row = self._fetchone(
            """
            UPDATE hosted_seller_api_keys
            SET status = 'revoked', revoked_at = %s
            WHERE seller_account_id = %s
              AND key_id = %s
              AND status = 'active'
              AND revoked_at IS NULL
            RETURNING key_id, seller_account_id, payment_profile_id, key_prefix,
                      status, created_at, revoked_at
            """,
            (utcnow(), seller_account_id, key_id),
        )
        self._commit()
        if row is None:
            raise KeyError("Seller API key is not active")
        return _seller_api_key_summary_from_mapping(row)

    def revoke_api_key_with_audit(
        self,
        *,
        seller_account_id: str,
        key_id: str,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> tuple[dict[str, Any], ControlPlaneAuditEvent]:
        now = utcnow()
        with self._transaction():
            row = self._fetchone(
                """
                UPDATE hosted_seller_api_keys
                SET status = 'revoked', revoked_at = %s
                WHERE seller_account_id = %s
                  AND key_id = %s
                  AND status = 'active'
                  AND revoked_at IS NULL
                RETURNING key_id, seller_account_id, payment_profile_id, key_prefix,
                          status, created_at, revoked_at
                """,
                (now, seller_account_id, key_id),
            )
            if row is None:
                raise KeyError("Seller API key is not active")
            key_summary = _seller_api_key_summary_from_mapping(row)
            audit_event = self._insert_seller_audit_event(
                action="seller_api_key_revoke",
                seller_account_id=seller_account_id,
                key_prefix_value=str(key_summary.get("keyPrefix") or ""),
                reason=reason,
                actor=actor,
                correlation_id=correlation_id,
                now=now,
            )
        return key_summary, audit_event

    def revoke_api_key_hash(self, key_hash: str) -> None:
        self._revoke_key("hosted_seller_api_keys", "key_hash", key_hash)

    def get_seller_account(self, seller_account_id: str) -> SellerAccountConfig | None:
        row = self._fetchone(
            """
            SELECT hosted_seller_accounts.*
            FROM hosted_seller_accounts
            JOIN hosted_tenants ON hosted_tenants.id = hosted_seller_accounts.tenant_id
            WHERE hosted_seller_accounts.id = %s
              AND hosted_seller_accounts.status = 'active'
              AND hosted_tenants.status = 'active'
            LIMIT 1
            """,
            (seller_account_id,),
        )
        if row is None:
            return None
        profile_rows = self._fetchall(
            """
            SELECT *
            FROM hosted_seller_payment_profiles
            WHERE seller_account_id = %s
              AND status = 'active'
            ORDER BY id ASC
            """,
            (seller_account_id,),
        )
        profiles = tuple(_payment_profile_config_from_mapping(row) for row in profile_rows)
        return _seller_account_config_from_mapping(row, payment_profiles=profiles)

    def _resolve_api_key(self, api_key: str) -> ResolvedSellerAccess:
        return self._resolve_key(
            table="hosted_seller_api_keys",
            hash_column="key_hash",
            key_hash_value=seller_key_hash(api_key),
            invalid_error=PermissionError("Invalid facilitator API key"),
        )

    def _resolve_key(
        self,
        *,
        table: str,
        hash_column: str,
        key_hash_value: str,
        invalid_error: Exception,
    ) -> ResolvedSellerAccess:
        row = self._fetchone(
            f"""
            SELECT
                hosted_seller_accounts.id,
                hosted_seller_accounts.tenant_id,
                hosted_seller_accounts.name,
                hosted_seller_accounts.environment,
                hosted_seller_accounts.status,
                hosted_seller_accounts.created_at,
                hosted_seller_accounts.updated_at,
                hosted_seller_payment_profiles.id AS profile_id,
                hosted_seller_payment_profiles.name AS profile_name,
                hosted_seller_payment_profiles.status AS profile_status,
                hosted_seller_payment_profiles.enabled_networks_json,
                hosted_seller_payment_profiles.enabled_schemes_json,
                hosted_seller_payment_profiles.enabled_providers_json,
                hosted_seller_payment_profiles.allowed_assets_json,
                hosted_seller_payment_profiles.allowed_pay_to_json,
                hosted_seller_payment_profiles.rate_limits_json,
                {table}.key_prefix AS api_key_prefix
            FROM {table}
            JOIN hosted_seller_accounts ON hosted_seller_accounts.id = {table}.seller_account_id
            JOIN hosted_seller_payment_profiles
              ON hosted_seller_payment_profiles.seller_account_id = {table}.seller_account_id
             AND hosted_seller_payment_profiles.id = {table}.payment_profile_id
            JOIN hosted_tenants ON hosted_tenants.id = hosted_seller_accounts.tenant_id
            WHERE {table}.{hash_column} = %s
              AND {table}.status = 'active'
              AND {table}.revoked_at IS NULL
              AND hosted_seller_payment_profiles.status = 'active'
              AND hosted_seller_accounts.status = 'active'
              AND hosted_tenants.status = 'active'
            LIMIT 1
            """,
            (key_hash_value,),
        )
        if row is None:
            raise invalid_error
        payment_profile = _payment_profile_config_from_mapping(row)
        seller_account = _seller_account_config_from_mapping(
            row, payment_profiles=(payment_profile,)
        )
        return ResolvedSellerAccess(
            seller_account=seller_account,
            payment_profile=payment_profile,
            api_key_prefix=str(row.get("api_key_prefix") or ""),
        )

    def _insert_key(
        self,
        *,
        table: str,
        hash_column: str,
        hash_value: str,
        key_id: str,
        seller_account_id: str,
        payment_profile_id: str,
        key_prefix_value: str,
    ) -> None:
        now = utcnow()
        with self._transaction():
            self._assert_active_payment_profile(
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
            )
            self._insert_seller_api_key_row(
                key_hash=hash_value,
                key_id=key_id,
                seller_account_id=seller_account_id,
                payment_profile_id=payment_profile_id,
                key_prefix_value=key_prefix_value,
                now=now,
                table=table,
                hash_column=hash_column,
            )

    def _assert_active_payment_profile(
        self, *, seller_account_id: str, payment_profile_id: str
    ) -> None:
        payment_profile = self._fetchone(
            """
            SELECT hosted_seller_payment_profiles.id
            FROM hosted_seller_payment_profiles
            JOIN hosted_seller_accounts
              ON hosted_seller_accounts.id = hosted_seller_payment_profiles.seller_account_id
            JOIN hosted_tenants ON hosted_tenants.id = hosted_seller_accounts.tenant_id
            WHERE hosted_seller_accounts.id = %s
              AND hosted_seller_payment_profiles.id = %s
              AND hosted_seller_payment_profiles.status = 'active'
              AND hosted_seller_accounts.status = 'active'
              AND hosted_tenants.status = 'active'
            FOR UPDATE OF hosted_seller_payment_profiles
            """,
            (seller_account_id, payment_profile_id),
        )
        if payment_profile is None:
            raise ValueError("Payment profile is not active")

    def _insert_seller_api_key_row(
        self,
        *,
        key_hash: str,
        key_id: str,
        seller_account_id: str,
        payment_profile_id: str,
        key_prefix_value: str,
        now: datetime,
        table: str = "hosted_seller_api_keys",
        hash_column: str = "key_hash",
    ) -> None:
        self._execute(
            f"""
            INSERT INTO {table} (
                {hash_column}, key_id, seller_account_id, payment_profile_id, key_prefix,
                status, created_at, revoked_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
            """,
            (
                key_hash,
                key_id,
                seller_account_id,
                payment_profile_id,
                key_prefix_value,
                "active",
                now,
            ),
        )

    def _insert_seller_audit_event(
        self,
        *,
        action: str,
        seller_account_id: str,
        key_prefix_value: str,
        reason: str | None = None,
        actor: str,
        correlation_id: str,
        now: datetime,
    ) -> ControlPlaneAuditEvent:
        safe_action = (
            "seller_create" if action == "seller_create" else _safe_seller_key_action(action)
        )
        row = self._fetchone(
            """
            INSERT INTO control_plane_audit_events (
                action, target_type, target, before_paused, after_paused,
                reason, actor, correlation_id, created_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING *
            """,
            (
                safe_action,
                ControlPlaneTargetType.SELLER.value,
                _safe_seller_ref(seller_account_id),
                safe_action == "seller_api_key_revoke",
                safe_action in {"seller_create", "seller_api_key_issue"},
                _seller_key_audit_reason(
                    key_prefix=key_prefix_value,
                    reason=reason if safe_action == "seller_api_key_revoke" else None,
                ),
                _safe_actor(actor),
                _safe_correlation_id(correlation_id),
                now,
            ),
        )
        if row is None:
            raise RuntimeError("Postgres seller audit event was not created")
        return _control_plane_audit_event_from_mapping(row)

    def _upsert_payment_profile(
        self, *, payment_profile: PaymentProfileConfig, now: datetime
    ) -> None:
        self._execute(
            """
            INSERT INTO hosted_seller_payment_profiles (
                id, seller_account_id, name, status,
                enabled_networks_json, enabled_schemes_json, enabled_providers_json,
                allowed_assets_json, allowed_pay_to_json, rate_limits_json,
                created_at, updated_at
            )
            VALUES (%s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb,
                    %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
            ON CONFLICT (seller_account_id, id) DO UPDATE
            SET name = EXCLUDED.name,
                status = EXCLUDED.status,
                enabled_networks_json = EXCLUDED.enabled_networks_json,
                enabled_schemes_json = EXCLUDED.enabled_schemes_json,
                enabled_providers_json = EXCLUDED.enabled_providers_json,
                allowed_assets_json = EXCLUDED.allowed_assets_json,
                allowed_pay_to_json = EXCLUDED.allowed_pay_to_json,
                rate_limits_json = EXCLUDED.rate_limits_json,
                updated_at = EXCLUDED.updated_at
            """,
            (
                payment_profile.payment_profile_id,
                payment_profile.seller_account_id,
                payment_profile.name,
                payment_profile.status,
                json.dumps(list(payment_profile.enabled_networks)),
                json.dumps(list(payment_profile.enabled_schemes)),
                json.dumps(list(payment_profile.enabled_providers)),
                json.dumps([value.lower() for value in payment_profile.allowed_assets]),
                json.dumps([value.lower() for value in payment_profile.allowed_pay_to]),
                json.dumps(_rate_limit_policy_json(payment_profile.rate_limits)),
                now,
                now,
            ),
        )

    def _upsert_bootstrap_api_key(
        self,
        *,
        api_key: str,
        seller_account_id: str,
        payment_profile_id: str,
        now: datetime,
    ) -> None:
        key_hash = seller_key_hash(api_key)
        existing = self._fetchone(
            """
            SELECT seller_account_id, payment_profile_id
            FROM hosted_seller_api_keys
            WHERE key_hash = %s
            FOR UPDATE
            """,
            (key_hash,),
        )
        if existing is not None:
            if (
                str(existing.get("seller_account_id")) != seller_account_id
                or str(existing.get("payment_profile_id")) != payment_profile_id
            ):
                raise ValueError("Duplicate seller API key configured")
            self._execute(
                """
                UPDATE hosted_seller_api_keys
                SET status = 'active',
                    key_prefix = %s,
                    revoked_at = NULL
                WHERE key_hash = %s
                """,
                (seller_key_prefix(api_key), key_hash),
            )
            return
        self._execute(
            """
            INSERT INTO hosted_seller_api_keys (
                key_hash, key_id, seller_account_id, payment_profile_id, key_prefix,
                status, created_at, revoked_at
            )
            VALUES (%s, %s, %s, %s, %s, %s, %s, NULL)
            """,
            (
                key_hash,
                f"key_{key_hash[:24]}",
                seller_account_id,
                payment_profile_id,
                seller_key_prefix(api_key),
                "active",
                now,
            ),
        )

    def _revoke_key(self, table: str, hash_column: str, hash_value: str) -> None:
        self._execute(
            f"""
            UPDATE {table}
            SET status = 'revoked', revoked_at = %s
            WHERE {hash_column} = %s
            """,
            (utcnow(), hash_value),
        )
        self._commit()

    def _transaction(self):
        transaction = getattr(self._conn, "transaction", None)
        if transaction is None:
            return nullcontext()
        return transaction()

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        return self._conn.execute(sql, params)

    def _commit(self) -> None:
        commit = getattr(self._conn, "commit", None)
        if commit is not None:
            commit()

    def _fetchone(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        result = self._execute(sql, params)
        if result is None:
            return None
        row = result.fetchone()
        return dict(row) if row is not None and not isinstance(row, dict) else row

    def _fetchall(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        result = self._execute(sql, params)
        if result is None:
            return []
        rows = result.fetchall()
        return [dict(row) if not isinstance(row, dict) else row for row in rows]


class PostgresSettlementStore:
    """Postgres-backed hosted settlement store.

    The store is approved for hosted mode because claims are durable and guarded by
    profile-scoped uniqueness plus provider-fingerprint advisory locks. The engine uses
    `claim_and_start_settle_attempt()` so the record claim and attempt row are created in
    one transaction before provider submission.
    """

    durable = True

    def __init__(
        self,
        dsn: str | None = None,
        *,
        connection: Any | None = None,
        hosted_safe: bool | None = None,
    ):
        if connection is None:
            if not dsn:
                raise ValueError("PostgresSettlementStore requires a DSN or connection")
            connection = _connect(dsn)
            self.hosted_safe = True if hosted_safe is None else hosted_safe
        else:
            self.hosted_safe = False if hosted_safe is None else hosted_safe
        self._conn = connection

    def initialize(self) -> None:
        apply_hosted_postgres_migrations(
            self._conn,
            scope="settlement",
            migrations=POSTGRES_SETTLEMENT_MIGRATIONS,
            validate_schema=self._schema_health_check,
        )

    def close(self) -> None:
        close = getattr(self._conn, "close", None)
        if close is not None:
            close()

    def health_check(self) -> bool:
        return self._schema_health_check() and hosted_postgres_migrations_current(
            self._conn,
            scope="settlement",
            migrations=POSTGRES_SETTLEMENT_MIGRATIONS,
        )

    def _schema_health_check(self) -> bool:
        row = self._fetchone(POSTGRES_HEALTH_SQL)
        if row is None:
            return False
        return all(
            bool(row.get(key))
            for key in (
                "settlement_records_table",
                "settlement_attempts_table",
                "seller_profile_fingerprint_unique",
                "no_stale_seller_fingerprint_unique",
                "status_updated_index",
                "provider_network_status_index",
                "reconciliation_lease_index",
                "attempts_record_started_index",
                "duplicate_claims_column",
                "last_duplicate_at_column",
                "last_duplicate_at_index",
                "provider_fingerprint_lookup_index",
                "seller_status_updated_index",
            )
        )

    def get(
        self,
        seller_account_id: str,
        fingerprint: str,
        *,
        payment_profile_id: str = "default",
    ) -> SettlementRecord | None:
        row = self._fetchone(
            """
            SELECT * FROM settlement_records
            WHERE seller_account_id = %s AND payment_profile_id = %s AND fingerprint = %s
            """,
            (seller_account_id, payment_profile_id, fingerprint),
        )
        return _record_from_mapping(row) if row is not None else None

    def get_by_provider_fingerprint(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        provider: str,
        scheme: str | None,
        network: str | None,
        fingerprint: str,
    ) -> SettlementRecord | None:
        row = self._fetchone(
            """
            SELECT *
            FROM settlement_records
            WHERE seller_account_id = %s
              AND provider = %s
              AND COALESCE(scheme, '') = COALESCE(%s, '')
              AND COALESCE(network, '') = COALESCE(%s, '')
              AND fingerprint = %s
            LIMIT 1
            """,
            (seller_account_id, provider, scheme, network, fingerprint),
        )
        return _record_from_mapping(row) if row is not None else None

    def count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS count FROM settlement_records")
        return int(row["count"])

    def settlement_status_counts(self) -> dict[str, int]:
        rows = self._fetchall(
            """
            SELECT status, COUNT(*) AS count
            FROM settlement_records
            GROUP BY status
            """
        )
        allowed = {status.value for status in SettlementStatus}
        return {
            str(row["status"]): int(row["count"]) for row in rows if str(row["status"]) in allowed
        }

    def settlement_provider_network_status_counts(self) -> list[dict[str, Any]]:
        rows = self._fetchall(
            """
            SELECT provider, COALESCE(network, '') AS network, status, COUNT(*) AS count
            FROM settlement_records
            GROUP BY provider, COALESCE(network, ''), status
            ORDER BY provider, network, status
            """
        )
        allowed = {status.value for status in SettlementStatus}
        return [
            {
                "provider": str(row["provider"]),
                "network": str(row["network"]),
                "status": str(row["status"]),
                "count": int(row["count"]),
            }
            for row in rows
            if str(row["status"]) in allowed
        ]

    def settlement_oldest_age_seconds(self) -> dict[str, int]:
        rows = self._fetchall(
            """
            SELECT status, EXTRACT(EPOCH FROM (NOW() - MIN(updated_at)))::BIGINT AS age_seconds
            FROM settlement_records
            GROUP BY status
            """
        )
        allowed = {status.value for status in SettlementStatus}
        return {
            str(row["status"]): max(0, int(row["age_seconds"] or 0))
            for row in rows
            if str(row["status"]) in allowed
        }

    def list_recent_records(self, *, limit: int = 25) -> list[SettlementRecord]:
        rows = self._fetchall(
            """
            SELECT *
            FROM settlement_records
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (max(0, min(int(limit), 100)),),
        )
        return [_record_from_mapping(row) for row in rows]

    def list_recent_records_for_seller(
        self, seller_account_id: str, *, limit: int = 25
    ) -> list[SettlementRecord]:
        rows = self._fetchall(
            """
            SELECT *
            FROM settlement_records
            WHERE seller_account_id = %s
            ORDER BY updated_at DESC, id DESC
            LIMIT %s
            """,
            (seller_account_id, max(0, min(int(limit), 100))),
        )
        return [_record_from_mapping(row) for row in rows]

    def list_reconciliation_queue(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
        limit: int = 50,
    ) -> list[SettlementRecord]:
        if not statuses:
            return []
        clauses = ["status = ANY(%s)"]
        params: list[Any] = [[str(status) for status in statuses]]
        if stale_before is not None:
            clauses.append("updated_at <= %s")
            params.append(stale_before)
        if seller_account_id is not None:
            clauses.append("seller_account_id = %s")
            params.append(seller_account_id)
        if provider is not None:
            clauses.append("provider = %s")
            params.append(provider)
        if network is not None:
            clauses.append("network = %s")
            params.append(network)
        params.append(max(0, min(int(limit), 100)))
        rows = self._fetchall(
            f"""
            SELECT *
            FROM settlement_records
            WHERE {" AND ".join(clauses)}
            ORDER BY updated_at ASC, id ASC
            LIMIT %s
            """,
            tuple(params),
        )
        return [_record_from_mapping(row) for row in rows]

    def reconciliation_queue_status_counts(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
        seller_account_id: str | None = None,
        provider: str | None = None,
        network: str | None = None,
    ) -> dict[str, int]:
        if not statuses:
            return {}
        clauses = ["status = ANY(%s)"]
        params: list[Any] = [[str(status) for status in statuses]]
        if stale_before is not None:
            clauses.append("updated_at <= %s")
            params.append(stale_before)
        if seller_account_id is not None:
            clauses.append("seller_account_id = %s")
            params.append(seller_account_id)
        if provider is not None:
            clauses.append("provider = %s")
            params.append(provider)
        if network is not None:
            clauses.append("network = %s")
            params.append(network)
        rows = self._fetchall(
            f"""
            SELECT status, COUNT(*) AS count
            FROM settlement_records
            WHERE {" AND ".join(clauses)}
            GROUP BY status
            """,
            tuple(params),
        )
        allowed = {status.value for status in SettlementStatus}
        return {
            str(row["status"]): int(row["count"]) for row in rows if str(row["status"]) in allowed
        }

    def settlement_status_counts_for_seller(self, seller_account_id: str) -> dict[str, int]:
        rows = self._fetchall(
            """
            SELECT status, COUNT(*) AS count
            FROM settlement_records
            WHERE seller_account_id = %s
            GROUP BY status
            """,
            (seller_account_id,),
        )
        allowed = {status.value for status in SettlementStatus}
        return {
            str(row["status"]): int(row["count"]) for row in rows if str(row["status"]) in allowed
        }

    def settlement_attempt_count_for_seller(self, seller_account_id: str) -> int:
        row = self._fetchone(
            """
            SELECT COUNT(*) AS count
            FROM settlement_attempts attempts
            JOIN settlement_records records ON records.id = attempts.settlement_record_id
            WHERE records.seller_account_id = %s
            """,
            (seller_account_id,),
        )
        return int(row["count"]) if row is not None else 0

    def settlement_amount_atomic_for_seller(self, seller_account_id: str) -> int:
        rows = self._fetchall(
            """
            SELECT raw_requirements_json
            FROM settlement_records
            WHERE seller_account_id = %s
              AND status = ANY(%s)
            """,
            (
                seller_account_id,
                [SettlementStatus.SETTLED.value, SettlementStatus.RECONCILED.value],
            ),
        )
        total = 0
        for row in rows:
            raw_requirements = row.get("raw_requirements_json") or {}
            if isinstance(raw_requirements, str):
                raw_requirements = json.loads(raw_requirements)
            if not isinstance(raw_requirements, dict):
                continue
            asset = str(raw_requirements.get("asset") or "").lower()
            if asset != "0x3600000000000000000000000000000000000000":
                continue
            amount = str(raw_requirements.get("amount") or "")
            if amount.isdigit():
                total += int(amount)
        return total

    def last_successful_settlement_at_for_seller(self, seller_account_id: str) -> datetime | None:
        row = self._fetchone(
            """
            SELECT MAX(updated_at) AS last_settlement_at
            FROM settlement_records
            WHERE seller_account_id = %s
              AND status = ANY(%s)
            """,
            (
                seller_account_id,
                [SettlementStatus.SETTLED.value, SettlementStatus.RECONCILED.value],
            ),
        )
        if row is None or row.get("last_settlement_at") is None:
            return None
        return _coerce_datetime(row["last_settlement_at"])

    def get_record_by_id(self, record_id: int) -> SettlementRecord | None:
        row = self._fetchone(
            """
            SELECT *
            FROM settlement_records
            WHERE id = %s
            """,
            (int(record_id),),
        )
        return _record_from_mapping(row) if row is not None else None

    def list_attempts_for_record(
        self, record_id: int, *, limit: int = 25
    ) -> list[SettlementAttemptRecord]:
        rows = self._fetchall(
            """
            SELECT *
            FROM settlement_attempts
            WHERE settlement_record_id = %s
            ORDER BY started_at DESC, id DESC
            LIMIT %s
            """,
            (int(record_id), max(0, min(int(limit), 100))),
        )
        return [_attempt_from_mapping(row) for row in rows]

    def attempt_count(self) -> int:
        row = self._fetchone("SELECT COUNT(*) AS count FROM settlement_attempts")
        return int(row["count"])

    def claim_for_settlement(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
    ) -> tuple[SettlementRecord, SettlementClaim]:
        record, claim, _ = self.claim_and_start_settle_attempt(
            seller_account_id=seller_account_id,
            payment_profile_id=payment_profile_id,
            fingerprint=fingerprint,
            provider=provider,
            scheme=scheme,
            network=network,
            trace_id=trace_id,
            raw_requirements=raw_requirements,
            start_attempt=False,
        )
        return record, claim

    def claim_and_start_settle_attempt(
        self,
        *,
        seller_account_id: str,
        payment_profile_id: str = "default",
        fingerprint: str,
        provider: str,
        scheme: str | None,
        network: str | None,
        trace_id: str,
        raw_requirements: dict[str, Any],
        start_attempt: bool = True,
    ) -> tuple[SettlementRecord, SettlementClaim, int | None]:
        now = utcnow()
        safe_requirements_json = json.dumps(_safe_requirements(raw_requirements), sort_keys=True)
        for attempt in range(_CLAIM_VISIBILITY_RETRIES):
            with self._transaction():
                self._execute(
                    PROVIDER_FINGERPRINT_LOCK_SQL,
                    (
                        f"{seller_account_id}:{provider}:{scheme or ''}:{network or ''}:{fingerprint}",
                    ),
                )
                row = self._fetchone(
                    """
                    SELECT *, FALSE AS inserted
                    FROM settlement_records
                    WHERE seller_account_id = %s
                      AND provider = %s
                      AND COALESCE(scheme, '') = COALESCE(%s, '')
                      AND COALESCE(network, '') = COALESCE(%s, '')
                      AND fingerprint = %s
                    ORDER BY id ASC
                    LIMIT 1
                    """,
                    (seller_account_id, provider, scheme, network, fingerprint),
                )
                if row is not None:
                    record = _record_from_mapping(row)
                    if record.record_id is not None:
                        self._execute(
                            """
                            UPDATE settlement_records
                            SET duplicate_claims = duplicate_claims + 1,
                                last_duplicate_at = %s
                            WHERE id = %s
                            """,
                            (now, record.record_id),
                        )
                    return record, _claim_for_status(record.status), None
                row = self._fetchone(
                    CLAIM_RECORD_SQL,
                    (
                        seller_account_id,
                        payment_profile_id,
                        fingerprint,
                        provider,
                        scheme,
                        network,
                        SettlementStatus.SETTLE_IN_PROGRESS,
                        trace_id,
                        safe_requirements_json,
                        now,
                        now,
                        seller_account_id,
                        payment_profile_id,
                        fingerprint,
                    ),
                )
                if row is None:
                    row = self._fetchone(
                        """
                        SELECT *, FALSE AS inserted
                        FROM settlement_records
                        WHERE seller_account_id = %s
                          AND payment_profile_id = %s
                          AND fingerprint = %s
                        """,
                        (seller_account_id, payment_profile_id, fingerprint),
                    )
                if row is None:
                    row = self._fetchone(
                        """
                        SELECT *, FALSE AS inserted
                        FROM settlement_records
                        WHERE seller_account_id = %s
                          AND provider = %s
                          AND COALESCE(scheme, '') = COALESCE(%s, '')
                          AND COALESCE(network, '') = COALESCE(%s, '')
                          AND fingerprint = %s
                        LIMIT 1
                        """,
                        (
                            seller_account_id,
                            provider,
                            scheme,
                            network,
                            fingerprint,
                        ),
                    )
                if row is not None:
                    record = _record_from_mapping(row)
                    inserted = bool(row.get("inserted"))
                    if not inserted:
                        if record.record_id is not None:
                            self._execute(
                                """
                                UPDATE settlement_records
                                SET duplicate_claims = duplicate_claims + 1,
                                    last_duplicate_at = %s
                                WHERE id = %s
                                """,
                                (now, record.record_id),
                            )
                        return record, _claim_for_status(record.status), None
                    if not start_attempt:
                        return record, SettlementClaim.CLAIMED, None
                    attempt_row = self._fetchone(
                        INSERT_ATTEMPT_SQL,
                        (
                            record.record_id,
                            trace_id,
                            SettlementAttemptStatus.STARTED,
                            now,
                        ),
                    )
                    if attempt_row is None:
                        raise RuntimeError("Postgres settlement attempt was not created")
                    return record, SettlementClaim.CLAIMED, int(attempt_row["id"])
            if attempt < _CLAIM_VISIBILITY_RETRIES - 1:
                time.sleep(_CLAIM_VISIBILITY_RETRY_SECONDS)
        raise RuntimeError("Postgres settlement claim did not return a record")

    def start_settle_attempt(self, record: SettlementRecord, *, trace_id: str) -> int:
        if record.record_id is None:
            raise ValueError("settlement record id is required for attempts")
        row = self._fetchone(
            INSERT_ATTEMPT_SQL,
            (record.record_id, trace_id, SettlementAttemptStatus.STARTED, utcnow()),
        )
        if row is None:
            raise RuntimeError("Postgres settlement attempt was not created")
        self._commit()
        return int(row["id"])

    def finish_settle_attempt(
        self,
        attempt_id: int,
        *,
        status: SettlementAttemptStatus,
        transaction: str | None = None,
        error_reason: str | None = None,
    ) -> None:
        self._execute(
            """
            UPDATE settlement_attempts
            SET status = %s, finished_at = %s, transaction_hash = %s, error_reason = %s
            WHERE id = %s
            """,
            (status, utcnow(), transaction, error_reason, attempt_id),
        )
        self._commit()

    def mark_record_and_finish_settle_attempt(
        self,
        record: SettlementRecord,
        *,
        attempt_id: int,
        record_status: str,
        attempt_status: SettlementAttemptStatus,
        transaction: str | None,
        payer: str | None,
        error_reason: str | None,
    ) -> None:
        status = SettlementStatus(record_status)
        if status == SettlementStatus.SETTLED:
            expected_statuses = (SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SUBMITTED)
        elif status in {SettlementStatus.SUBMITTED, SettlementStatus.SETTLE_FAILED}:
            expected_statuses = (SettlementStatus.SETTLE_IN_PROGRESS,)
        elif status == SettlementStatus.UNKNOWN:
            expected_statuses = (SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SUBMITTED)
        else:
            raise ValueError(f"Unsupported settlement finalization status: {record_status}")
        if record.record_id is None:
            raise ValueError("settlement record id is required for updates")
        updated_at = utcnow()
        with self._transaction():
            cursor = self._execute(
                """
                UPDATE settlement_records
                SET status = %s,
                    transaction_hash = %s,
                    payer = %s,
                    error_reason = %s,
                    reconciliation_owner = NULL,
                    reconciliation_lease_until = NULL,
                    updated_at = %s
                WHERE id = %s AND status = ANY(%s)
                """,
                (
                    status,
                    transaction,
                    payer,
                    error_reason,
                    updated_at,
                    record.record_id,
                    [str(expected) for expected in expected_statuses],
                ),
            )
            if getattr(cursor, "rowcount", None) == 0:
                raise RuntimeError(
                    f"Postgres settlement record {record.record_id} was not in an expected "
                    f"status for transition to {status}"
                )
            self._execute(
                """
                UPDATE settlement_attempts
                SET status = %s, finished_at = %s, transaction_hash = %s, error_reason = %s
                WHERE id = %s
                """,
                (attempt_status, updated_at, transaction, error_reason, attempt_id),
            )
        record.status = status
        record.transaction = transaction
        record.payer = payer
        record.error_reason = error_reason
        record.updated_at = updated_at
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None

    def mark_settled(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SETTLED,
            expected_statuses=(SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SUBMITTED),
            transaction=transaction,
            payer=payer,
            error_reason=None,
        )

    def mark_settle_failed(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SETTLE_FAILED,
            expected_statuses=(SettlementStatus.SETTLE_IN_PROGRESS,),
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
        )

    def mark_submitted(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
    ) -> None:
        self._update_record(
            record,
            status=SettlementStatus.SUBMITTED,
            expected_statuses=(SettlementStatus.SETTLE_IN_PROGRESS,),
            transaction=transaction,
            payer=payer,
            error_reason=None,
        )

    def mark_unknown(self, record: SettlementRecord, *, error_reason: str | None) -> None:
        self._update_record(
            record,
            status=SettlementStatus.UNKNOWN,
            expected_statuses=(SettlementStatus.SETTLE_IN_PROGRESS, SettlementStatus.SUBMITTED),
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
        )

    def list_reconciliation_candidates(
        self,
        *,
        statuses: tuple[SettlementStatus, ...],
        stale_before: datetime,
        limit: int = 100,
    ) -> list[SettlementRecord]:
        if not statuses:
            return []
        rows = self._fetchall(
            LIST_RECONCILIATION_CANDIDATES_SQL,
            (
                [str(status) for status in statuses],
                stale_before,
                utcnow(),
                limit,
            ),
        )
        return [_record_from_mapping(row) for row in rows]

    def claim_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
        lease_until: datetime,
        eligible_statuses: tuple[SettlementStatus, ...],
        stale_before: datetime | None = None,
    ) -> SettlementRecord | None:
        if not owner:
            raise ValueError("reconciliation owner is required")
        if not eligible_statuses:
            return None
        row = self._fetchone(
            CLAIM_RECONCILIATION_RECORD_SQL,
            (
                owner,
                lease_until,
                record_id,
                [str(status) for status in eligible_statuses],
                stale_before,
                stale_before,
                utcnow(),
            ),
        )
        self._commit()
        return _record_from_mapping(row) if row is not None else None

    def release_reconciliation_record(
        self,
        *,
        record_id: int,
        owner: str,
    ) -> SettlementRecord | None:
        if not owner:
            raise ValueError("reconciliation owner is required")
        row = self._fetchone(
            """
            UPDATE settlement_records
            SET reconciliation_owner = NULL,
                reconciliation_lease_until = NULL,
                updated_at = %s
            WHERE id = %s
              AND reconciliation_owner = %s
            RETURNING *
            """,
            (utcnow(), int(record_id), owner),
        )
        self._commit()
        return _record_from_mapping(row) if row is not None else None

    def apply_reconciliation_control_action(
        self,
        *,
        action: str,
        record_id: int,
        owner: str,
        lease_until: datetime,
        active_stale_before: datetime,
        reason: str,
        actor: str,
        correlation_id: str,
    ) -> tuple[SettlementRecord, ControlPlaneAuditEvent]:
        safe_action = _safe_reconciliation_action(action)
        safe_owner = _safe_operator_lease_owner(owner)
        if not safe_owner:
            raise ValueError("reconciliation owner is required")
        now = utcnow()
        try:
            with self._transaction():
                locked = self._fetchone(
                    """
                    SELECT *
                    FROM settlement_records
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (int(record_id),),
                )
                if locked is None:
                    raise KeyError(record_id)
                record = _record_from_mapping(locked)
                before_active = bool(record.reconciliation_owner)
                if safe_action == "reconciliation_claim":
                    updated = self._claim_reconciliation_record_locked(
                        record=record,
                        owner=safe_owner,
                        lease_until=lease_until,
                        active_stale_before=active_stale_before,
                        now=now,
                    )
                    after_active = True
                elif safe_action == "reconciliation_release":
                    updated = self._release_reconciliation_record_locked(
                        record=record,
                        owner=safe_owner,
                        now=now,
                    )
                    after_active = False
                elif safe_action == "reconciliation_manual_review":
                    updated = self._manual_review_reconciliation_record_locked(
                        record=record,
                        owner=safe_owner,
                        now=now,
                    )
                    after_active = True
                else:
                    raise RuntimeError("Unsupported reconciliation action")
                audit_row = self._fetchone(
                    """
                    INSERT INTO control_plane_audit_events (
                        action, target_type, target, before_paused, after_paused,
                        reason, actor, correlation_id, created_at
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING *
                    """,
                    (
                        safe_action,
                        ControlPlaneTargetType.SETTLEMENT.value,
                        str(int(record_id)),
                        before_active,
                        after_active,
                        _safe_reason(reason),
                        _safe_actor(actor),
                        _safe_correlation_id(correlation_id),
                        now,
                    ),
                )
                if audit_row is None:
                    raise RuntimeError(
                        "Postgres control-plane reconciliation audit event was not created"
                    )
                return updated, _control_plane_audit_event_from_mapping(audit_row)
        except Exception:
            self._rollback()
            raise

    def _claim_reconciliation_record_locked(
        self,
        *,
        record: SettlementRecord,
        owner: str,
        lease_until: datetime,
        active_stale_before: datetime,
        now: datetime,
    ) -> SettlementRecord:
        if record.record_id is None:
            raise ValueError("settlement record id is required")
        if record.status not in {
            SettlementStatus.UNKNOWN,
            SettlementStatus.MANUAL_REVIEW,
            SettlementStatus.SUBMITTED,
            SettlementStatus.SETTLE_IN_PROGRESS,
        }:
            raise RuntimeError("Settlement record is not claimable")
        if (
            record.status in {SettlementStatus.SUBMITTED, SettlementStatus.SETTLE_IN_PROGRESS}
            and record.updated_at > active_stale_before
        ):
            raise RuntimeError("Active settlement record is not stale enough to claim")
        if (
            record.reconciliation_lease_until is not None
            and record.reconciliation_lease_until > now
        ):
            raise RuntimeError("Settlement record is not claimable")
        row = self._fetchone(
            """
            UPDATE settlement_records
            SET reconciliation_owner = %s,
                reconciliation_lease_until = %s,
                reconciliation_attempts = reconciliation_attempts + 1,
                updated_at = %s
            WHERE id = %s
            RETURNING *
            """,
            (owner, lease_until, now, record.record_id),
        )
        if row is None:
            raise RuntimeError("Settlement record is not claimable")
        return _record_from_mapping(row)

    def _release_reconciliation_record_locked(
        self,
        *,
        record: SettlementRecord,
        owner: str,
        now: datetime,
    ) -> SettlementRecord:
        if record.record_id is None:
            raise ValueError("settlement record id is required")
        if record.reconciliation_owner != owner:
            raise RuntimeError("Settlement record is not leased by this operator")
        row = self._fetchone(
            """
            UPDATE settlement_records
            SET reconciliation_owner = NULL,
                reconciliation_lease_until = NULL,
                updated_at = %s
            WHERE id = %s
              AND reconciliation_owner = %s
            RETURNING *
            """,
            (now, record.record_id, owner),
        )
        if row is None:
            raise RuntimeError("Settlement record is not leased by this operator")
        return _record_from_mapping(row)

    def _manual_review_reconciliation_record_locked(
        self,
        *,
        record: SettlementRecord,
        owner: str,
        now: datetime,
    ) -> SettlementRecord:
        if record.record_id is None:
            raise ValueError("settlement record id is required")
        if record.status != SettlementStatus.UNKNOWN:
            raise RuntimeError("Only unknown settlement records can move to manual review")
        if record.reconciliation_owner != owner:
            raise RuntimeError("Settlement record must be claimed by this operator")
        if record.reconciliation_lease_until is None or record.reconciliation_lease_until <= now:
            raise RuntimeError("Settlement record must be claimed by this operator")
        row = self._fetchone(
            """
            UPDATE settlement_records
            SET status = %s,
                error_reason = %s,
                reconciliation_owner = NULL,
                reconciliation_lease_until = NULL,
                updated_at = %s
            WHERE id = %s
              AND status = %s
              AND reconciliation_owner = %s
              AND reconciliation_lease_until = %s
              AND reconciliation_lease_until > %s
            RETURNING *
            """,
            (
                SettlementStatus.MANUAL_REVIEW,
                "manual_review_operator_requested",
                now,
                record.record_id,
                SettlementStatus.UNKNOWN,
                owner,
                record.reconciliation_lease_until,
                now,
            ),
        )
        if row is None:
            raise RuntimeError("Settlement record must be claimed by this operator")
        self._finish_started_attempts_for_reconciliation(
            record_id=record.record_id,
            status=SettlementStatus.MANUAL_REVIEW,
            finished_at=now,
            transaction=record.transaction,
            error_reason="manual_review_operator_requested",
        )
        return _record_from_mapping(row)

    def list_stale_started_attempts(
        self,
        *,
        started_before: datetime,
        limit: int = 100,
    ) -> list[SettlementAttemptRecord]:
        rows = self._fetchall(
            LIST_STALE_STARTED_ATTEMPTS_SQL,
            (SettlementAttemptStatus.STARTED, started_before, limit),
        )
        return [_attempt_from_mapping(row) for row in rows]

    def mark_reconciled_settled(
        self,
        record: SettlementRecord,
        *,
        transaction: str | None,
        payer: str | None,
        owner: str,
    ) -> None:
        self._update_reconciliation_record(
            record,
            status=SettlementStatus.SETTLED,
            expected_statuses=(
                SettlementStatus.UNKNOWN,
                SettlementStatus.SUBMITTED,
                SettlementStatus.SETTLE_IN_PROGRESS,
            ),
            transaction=transaction,
            payer=payer,
            error_reason=None,
            owner=owner,
        )

    def mark_reconciled_failed(
        self,
        record: SettlementRecord,
        *,
        error_reason: str | None,
        owner: str,
    ) -> None:
        self._update_reconciliation_record(
            record,
            status=SettlementStatus.SETTLE_FAILED,
            expected_statuses=(
                SettlementStatus.UNKNOWN,
                SettlementStatus.SETTLE_IN_PROGRESS,
            ),
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
            owner=owner,
        )

    def mark_reconciled_unknown(
        self,
        record: SettlementRecord,
        *,
        error_reason: str | None,
        owner: str,
    ) -> None:
        self._update_reconciliation_record(
            record,
            status=SettlementStatus.UNKNOWN,
            expected_statuses=(SettlementStatus.SUBMITTED, SettlementStatus.SETTLE_IN_PROGRESS),
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
            owner=owner,
        )

    def mark_manual_review(
        self,
        record: SettlementRecord,
        *,
        error_reason: str | None,
        owner: str,
    ) -> None:
        self._update_reconciliation_record(
            record,
            status=SettlementStatus.MANUAL_REVIEW,
            expected_statuses=(SettlementStatus.UNKNOWN,),
            transaction=record.transaction,
            payer=record.payer,
            error_reason=error_reason,
            owner=owner,
        )

    def _update_record(
        self,
        record: SettlementRecord,
        *,
        status: SettlementStatus,
        expected_statuses: tuple[SettlementStatus, ...],
        transaction: str | None,
        payer: str | None,
        error_reason: str | None,
    ) -> None:
        if record.record_id is None:
            raise ValueError("settlement record id is required for updates")
        updated_at = utcnow()
        cursor = self._execute(
            """
            UPDATE settlement_records
            SET status = %s,
                transaction_hash = %s,
                payer = %s,
                error_reason = %s,
                reconciliation_owner = NULL,
                reconciliation_lease_until = NULL,
                updated_at = %s
            WHERE id = %s AND status = ANY(%s)
            """,
            (
                status,
                transaction,
                payer,
                error_reason,
                updated_at,
                record.record_id,
                [str(expected) for expected in expected_statuses],
            ),
        )
        if getattr(cursor, "rowcount", None) == 0:
            self._rollback()
            raise RuntimeError(
                f"Postgres settlement record {record.record_id} was not in an expected "
                f"status for transition to {status}"
            )
        self._commit()
        record.status = status
        record.transaction = transaction
        record.payer = payer
        record.error_reason = error_reason
        record.updated_at = updated_at
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None

    def _update_reconciliation_record(
        self,
        record: SettlementRecord,
        *,
        status: SettlementStatus,
        expected_statuses: tuple[SettlementStatus, ...],
        transaction: str | None,
        payer: str | None,
        error_reason: str | None,
        owner: str,
    ) -> None:
        if record.record_id is None:
            raise ValueError("settlement record id is required for updates")
        if not owner:
            raise ValueError("reconciliation owner is required")
        if record.reconciliation_lease_until is None:
            raise ValueError("claimed reconciliation lease is required for updates")
        updated_at = utcnow()

        def apply_update() -> None:
            cursor = self._execute(
                """
                UPDATE settlement_records
                SET status = %s,
                    transaction_hash = %s,
                    payer = %s,
                    error_reason = %s,
                    reconciliation_owner = NULL,
                    reconciliation_lease_until = NULL,
                    updated_at = %s
                WHERE id = %s
                  AND status = ANY(%s)
                  AND reconciliation_owner = %s
                  AND reconciliation_lease_until = %s
                  AND reconciliation_lease_until > %s
                """,
                (
                    status,
                    transaction,
                    payer,
                    error_reason,
                    updated_at,
                    record.record_id,
                    [str(expected) for expected in expected_statuses],
                    owner,
                    record.reconciliation_lease_until,
                    updated_at,
                ),
            )
            if getattr(cursor, "rowcount", None) == 0:
                raise RuntimeError(
                    f"Postgres settlement record {record.record_id} was not leased by {owner} "
                    f"in an expected status for transition to {status}"
                )
            self._finish_started_attempts_for_reconciliation(
                record_id=record.record_id,
                status=status,
                finished_at=updated_at,
                transaction=transaction,
                error_reason=error_reason,
            )

        try:
            transaction_context = getattr(self._conn, "transaction", None)
            if transaction_context is None:
                apply_update()
                self._commit()
            else:
                with transaction_context():
                    apply_update()
        except Exception:
            self._rollback()
            raise
        record.status = status
        record.transaction = transaction
        record.payer = payer
        record.error_reason = error_reason
        record.updated_at = updated_at
        record.reconciliation_owner = None
        record.reconciliation_lease_until = None

    def _finish_started_attempts_for_reconciliation(
        self,
        *,
        record_id: int,
        status: SettlementStatus,
        finished_at: datetime,
        transaction: str | None,
        error_reason: str | None,
    ) -> None:
        attempt_status = _attempt_status_for_reconciled_status(status)
        if attempt_status is None:
            return
        self._execute(
            """
            UPDATE settlement_attempts
            SET status = %s,
                finished_at = %s,
                transaction_hash = COALESCE(%s, transaction_hash),
                error_reason = COALESCE(%s, error_reason)
            WHERE settlement_record_id = %s
              AND status = %s
            """,
            (
                attempt_status,
                finished_at,
                transaction,
                error_reason,
                record_id,
                SettlementAttemptStatus.STARTED,
            ),
        )

    def _transaction(self):
        transaction = getattr(self._conn, "transaction", None)
        if transaction is None:
            return nullcontext()
        return transaction()

    def _execute(self, sql: str, params: tuple[Any, ...] | None = None) -> Any:
        return self._conn.execute(sql, params)

    def _commit(self) -> None:
        commit = getattr(self._conn, "commit", None)
        if commit is not None:
            commit()

    def _rollback(self) -> None:
        rollback = getattr(self._conn, "rollback", None)
        if rollback is not None:
            rollback()

    def _fetchone(self, sql: str, params: tuple[Any, ...] | None = None) -> dict[str, Any] | None:
        result = self._execute(sql, params)
        if result is None:
            return None
        row = result.fetchone()
        return dict(row) if row is not None and not isinstance(row, dict) else row

    def _fetchall(self, sql: str, params: tuple[Any, ...] | None = None) -> list[dict[str, Any]]:
        result = self._execute(sql, params)
        if result is None:
            return []
        rows = result.fetchall()
        return [dict(row) if not isinstance(row, dict) else row for row in rows]


def _record_from_mapping(row: dict[str, Any]) -> SettlementRecord:
    raw_requirements = row.get("raw_requirements_json") or {}
    if isinstance(raw_requirements, str):
        raw_requirements = json.loads(raw_requirements)
    return SettlementRecord(
        record_id=int(row["id"]),
        seller_account_id=row["seller_account_id"],
        payment_profile_id=str(row.get("payment_profile_id") or "default"),
        fingerprint=row["fingerprint"],
        provider=row["provider"],
        scheme=row.get("scheme"),
        network=row.get("network"),
        status=SettlementStatus(row["status"]),
        trace_id=row.get("trace_id"),
        transaction=row.get("transaction_hash"),
        payer=row.get("payer"),
        error_reason=row.get("error_reason"),
        created_at=_coerce_datetime(row["created_at"]),
        updated_at=_coerce_datetime(row["updated_at"]),
        raw_requirements=raw_requirements,
        reconciliation_owner=row.get("reconciliation_owner"),
        reconciliation_lease_until=(
            _coerce_datetime(row["reconciliation_lease_until"])
            if row.get("reconciliation_lease_until") is not None
            else None
        ),
        reconciliation_attempts=int(row.get("reconciliation_attempts") or 0),
        duplicate_claims=int(row.get("duplicate_claims") or 0),
        last_duplicate_at=(
            _coerce_datetime(row["last_duplicate_at"])
            if row.get("last_duplicate_at") is not None
            else None
        ),
    )


def _attempt_from_mapping(row: dict[str, Any]) -> SettlementAttemptRecord:
    return SettlementAttemptRecord(
        attempt_id=int(row["id"]),
        settlement_record_id=int(row["settlement_record_id"]),
        trace_id=row.get("trace_id"),
        status=SettlementAttemptStatus(row["status"]),
        started_at=_coerce_datetime(row["started_at"]),
        finished_at=(
            _coerce_datetime(row["finished_at"]) if row.get("finished_at") is not None else None
        ),
        transaction=row.get("transaction_hash"),
        error_reason=row.get("error_reason"),
    )


def _attempt_status_for_reconciled_status(
    status: SettlementStatus,
) -> SettlementAttemptStatus | None:
    if status == SettlementStatus.SETTLED:
        return SettlementAttemptStatus.SETTLED
    if status == SettlementStatus.SETTLE_FAILED:
        return SettlementAttemptStatus.FAILED
    if status in {SettlementStatus.UNKNOWN, SettlementStatus.MANUAL_REVIEW}:
        return SettlementAttemptStatus.UNKNOWN
    return None


def _control_plane_audit_event_from_mapping(row: dict[str, Any]) -> ControlPlaneAuditEvent:
    action = str(row["action"])
    reason = str(row.get("reason") or "")
    return ControlPlaneAuditEvent(
        event_id=int(row["id"]),
        action=action,
        target_type=_control_plane_target_type(row["target_type"]),
        target=str(row["target"]),
        before=bool(row["before_paused"]),
        after=bool(row["after_paused"]),
        reason=(
            _safe_audit_reason(reason)
            if action in {"seller_create", "seller_api_key_issue", "seller_api_key_revoke"}
            else _safe_reason(reason)
        ),
        actor=_safe_actor(str(row.get("actor") or "")),
        correlation_id=_safe_correlation_id(str(row.get("correlation_id") or "")),
        created_at=_coerce_datetime(row["created_at"]),
    )


def _control_plane_target_type(value: Any) -> ControlPlaneTargetType:
    target_type = str(value)
    if target_type == "project":
        return ControlPlaneTargetType.SELLER
    return ControlPlaneTargetType(target_type)


def _payment_profile_config_from_mapping(row: dict[str, Any]) -> PaymentProfileConfig:
    profile_id = row.get("profile_id", row.get("id", "default"))
    profile_name = row.get("profile_name", row.get("name", profile_id))
    profile_status = row.get("profile_status", row.get("status", "disabled"))
    return PaymentProfileConfig(
        payment_profile_id=str(profile_id),
        seller_account_id=str(
            row["seller_account_id"] if "seller_account_id" in row else row["id"]
        ),
        name=str(profile_name or profile_id),
        status=str(profile_status or "disabled"),
        enabled_networks=tuple(_json_string_list(row.get("enabled_networks_json"))),
        enabled_schemes=tuple(_json_string_list(row.get("enabled_schemes_json"))),
        enabled_providers=tuple(_json_string_list(row.get("enabled_providers_json"))),
        allowed_assets=tuple(
            value.lower() for value in _json_string_list(row.get("allowed_assets_json"))
        ),
        allowed_pay_to=tuple(
            value.lower() for value in _json_string_list(row.get("allowed_pay_to_json"))
        ),
        rate_limits=_rate_limit_policy_from_json(row.get("rate_limits_json")),
    )


def _seller_account_config_from_mapping(
    row: dict[str, Any],
    *,
    payment_profiles: tuple[PaymentProfileConfig, ...] = (),
) -> SellerAccountConfig:
    if not payment_profiles and "enabled_networks_json" in row:
        payment_profiles = (_payment_profile_config_from_mapping(row),)
    return SellerAccountConfig(
        seller_account_id=str(row["id"]),
        tenant_id=str(row["tenant_id"]),
        name=str(row.get("name") or row["id"]),
        environment=str(row.get("environment") or "testnet"),
        status=str(row.get("status") or "disabled"),
        payment_profiles=payment_profiles,
    )


def _seller_admin_summary_from_mapping(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "sellerRef": str(row["id"]),
        "tenantRef": str(row["tenant_id"]),
        "name": str(row.get("name") or row["id"]),
        "environment": str(row.get("environment") or "testnet"),
        "status": str(row.get("status") or "disabled"),
        "paymentProfileCount": max(0, int(row.get("payment_profile_count") or 0)),
        "activeApiKeyCount": max(0, int(row.get("active_api_key_count") or 0)),
        "createdAt": _coerce_datetime(row["created_at"]).isoformat(),
        "updatedAt": _coerce_datetime(row["updated_at"]).isoformat(),
    }


def _seller_api_key_summary_from_mapping(row: dict[str, Any]) -> dict[str, Any]:
    revoked_at = row.get("revoked_at")
    return {
        "keyId": str(row["key_id"]),
        "sellerRef": str(row["seller_account_id"]),
        "paymentProfileId": str(row.get("payment_profile_id") or "default"),
        "keyPrefix": str(row.get("key_prefix") or ""),
        "status": str(row.get("status") or "disabled"),
        "createdAt": _coerce_datetime(row["created_at"]).isoformat(),
        "revokedAt": _coerce_datetime(revoked_at).isoformat() if revoked_at is not None else None,
    }


class PostgresSettlementStorePool:
    """Small bounded connection pool for hosted settlement store factories."""

    hosted_safe = True
    durable = True

    def __init__(self, dsn: str, *, max_size: int = 10, checkout_timeout_seconds: float = 2.0):
        if max_size <= 0:
            raise ValueError("max_size must be positive")
        if checkout_timeout_seconds <= 0:
            raise ValueError("checkout_timeout_seconds must be positive")
        self._dsn = dsn
        self._max_size = max_size
        self._checkout_timeout_seconds = checkout_timeout_seconds
        self._idle: queue.LifoQueue[Any] = queue.LifoQueue(maxsize=max_size)
        self._lock = threading.Lock()
        self._total = 0
        self._closed = False

    def __call__(self) -> PostgresSettlementStore:
        return self.store()

    def store(self) -> PostgresSettlementStore:
        connection = self._checkout()
        return PostgresSettlementStore(
            connection=_PooledPostgresConnection(self, connection),
            hosted_safe=True,
        )

    def initialize(self) -> None:
        store = self.store()
        try:
            store.initialize()
        finally:
            store.close()

    def health_check(self) -> bool:
        store = self.store()
        try:
            return store.health_check()
        finally:
            store.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
        while True:
            try:
                connection = self._idle.get_nowait()
            except queue.Empty:
                return
            _close_connection(connection)

    def _checkout(self) -> Any:
        with self._lock:
            if self._closed:
                raise RuntimeError("Postgres settlement store pool is closed")
            try:
                return self._idle.get_nowait()
            except queue.Empty:
                if self._total < self._max_size:
                    self._total += 1
                    create = True
                else:
                    create = False
        if create:
            try:
                return _connect(self._dsn)
            except Exception:
                with self._lock:
                    self._total -= 1
                raise
        try:
            return self._idle.get(timeout=self._checkout_timeout_seconds)
        except queue.Empty as exc:
            raise RuntimeError("Postgres settlement store pool checkout timed out") from exc

    def _release(self, connection: Any) -> None:
        with self._lock:
            closed = self._closed
        if closed:
            _close_connection(connection)
            with self._lock:
                self._total -= 1
            return
        try:
            self._idle.put_nowait(connection)
        except queue.Full:
            _close_connection(connection)
            with self._lock:
                self._total -= 1


class _PooledPostgresConnection:
    def __init__(self, pool: PostgresSettlementStorePool, connection: Any):
        self._pool = pool
        self._connection = connection
        self._released = False

    def close(self) -> None:
        if self._released:
            return
        self._released = True
        self._pool._release(self._connection)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._connection, name)


def _json_string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if isinstance(item, str) and item]


def _rate_limit_policy_json(policy: RateLimitPolicy) -> dict[str, int | None]:
    return {
        "sellerRequestsPerMinute": policy.seller_requests_per_minute,
        "supportedPerMinute": policy.supported_per_minute,
        "verifyPerMinute": policy.verify_per_minute,
        "settlePerMinute": policy.settle_per_minute,
        "invalidRequestsPerMinute": policy.invalid_requests_per_minute,
        "ipRequestsPerMinute": policy.ip_requests_per_minute,
    }


def _rate_limit_policy_from_json(value: Any) -> RateLimitPolicy:
    if value is None:
        return DEFAULT_RATE_LIMIT_POLICY
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        return DEFAULT_RATE_LIMIT_POLICY
    return RateLimitPolicy(
        seller_requests_per_minute=_optional_int(value.get("sellerRequestsPerMinute")),
        supported_per_minute=_optional_int(value.get("supportedPerMinute")),
        verify_per_minute=_optional_int(value.get("verifyPerMinute")),
        settle_per_minute=_optional_int(value.get("settlePerMinute")),
        invalid_requests_per_minute=_optional_int(value.get("invalidRequestsPerMinute")),
        ip_requests_per_minute=_optional_int(value.get("ipRequestsPerMinute")),
    )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    return None


def _coerce_datetime(value: datetime | str) -> datetime:
    if isinstance(value, datetime):
        return value
    return _parse_datetime(value)


def _connect(dsn: str) -> Any:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ImportError as exc:
        raise RuntimeError(
            "PostgresSettlementStore requires psycopg. Install with `psycopg[binary]`."
        ) from exc
    return psycopg.connect(dsn, row_factory=dict_row, autocommit=True)


def _close_connection(connection: Any) -> None:
    close = getattr(connection, "close", None)
    if close is not None:
        close()


def postgres_store_from_env() -> PostgresSettlementStore:
    dsn = os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN") or os.getenv("DATABASE_URL")
    if not dsn:
        raise RuntimeError(
            "Postgres settlement store requires OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN "
            "or DATABASE_URL"
        )
    return PostgresSettlementStore(dsn)


def postgres_control_plane_state_from_env() -> PostgresControlPlaneState:
    dsn = (
        os.getenv("OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN")
        or os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN")
        or os.getenv("DATABASE_URL")
    )
    if not dsn:
        raise RuntimeError(
            "Postgres control-plane state requires OMNICLAW_HOSTED_CONTROL_PLANE_POSTGRES_DSN, "
            "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN, or DATABASE_URL"
        )
    return PostgresControlPlaneState(dsn)


def postgres_seller_account_resolver_from_env() -> PostgresSellerAccountResolver:
    dsn = (
        os.getenv("OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN")
        or os.getenv("OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN")
        or os.getenv("DATABASE_URL")
    )
    if not dsn:
        raise RuntimeError(
            "Postgres seller account resolver requires OMNICLAW_HOSTED_SELLER_ACCOUNTS_POSTGRES_DSN, "
            "OMNICLAW_HOSTED_FACILITATOR_POSTGRES_DSN, or DATABASE_URL"
        )
    return PostgresSellerAccountResolver(dsn)
