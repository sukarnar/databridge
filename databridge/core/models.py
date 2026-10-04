"""ORM models for DataBridge metadata.

Chain: Connection -> SourceObject -> Snapshot -> Mapping (+ TargetSchema) -> Dataset -> Endpoint -> ApiKey.
Large data never lives here: snapshots and datasets are Parquet files referenced by path.
"""

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import JSON, Boolean, DateTime, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from databridge.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class Connection(TimestampMixin, Base):
    """Where data lives and how to reach it. Secrets are stored encrypted."""

    __tablename__ = "connections"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    type: Mapped[str] = mapped_column(String(40))  # connector type_name: filesystem | database
    config: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SourceObject(TimestampMixin, Base):
    """Anything that yields rows: an uploaded file, a file on a share, a table, a view or a SQL query."""

    __tablename__ = "source_objects"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(20))  # upload | file | table | sql | rest | workflow
    connection_id: Mapped[Optional[int]] = mapped_column(ForeignKey("connections.id"), nullable=True)
    object_ref: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    sheet_profile: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    latest_snapshot_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Data classification per column for AI egress rules: {column: public | internal | pii | confidential}
    classification: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)
    # Built-in schedule for connection-backed sources (REST, tables, SQL, files): minutes between refreshes
    refresh_minutes: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    last_refresh_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_ok_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_refresh_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)  # ok | unchanged | failed
    last_refresh_message: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Snapshot(Base):
    """One immutable ingest of raw source data, stored as Parquet."""

    __tablename__ = "snapshots"

    id: Mapped[int] = mapped_column(primary_key=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_objects.id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    file_name: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    file_hash: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    parquet_path: Mapped[str] = mapped_column(Text)
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)
    drift: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)


class TargetSchema(TimestampMixin, Base):
    """Target structure. fields = [{name, type, required, description}]."""

    __tablename__ = "target_schemas"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    origin: Mapped[str] = mapped_column(String(20), default="manual")  # manual | template | source
    fields: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)


class Mapping(TimestampMixin, Base):
    """How a source becomes a target.

    rules       = [{target, formula}]         one arrow group per target field
    row_steps   = [{kind, ...}]               filter / dedupe / sort / unpivot before column rules
    validations = [{field, kind, value}]      extra data-quality rules on target fields
    """

    __tablename__ = "mappings"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    source_id: Mapped[int] = mapped_column(ForeignKey("source_objects.id"))
    target_id: Mapped[int] = mapped_column(ForeignKey("target_schemas.id"))
    rules: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    row_steps: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    validations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(20), default="draft")  # draft | published
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    published_spec: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    auto_publish: Mapped[bool] = mapped_column(Boolean, default=True)


class Dataset(Base):
    """Curated, versioned output of a published mapping."""

    __tablename__ = "datasets"

    id: Mapped[int] = mapped_column(primary_key=True)
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id"))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    parquet_path: Mapped[str] = mapped_column(Text)
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    rejected_count: Mapped[int] = mapped_column(Integer, default=0)
    reject_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    run_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class Endpoint(TimestampMixin, Base):
    """A published REST route serving a mapping's datasets.

    params = [{name, column, op}] where op in eq | ne | gt | gte | lt | lte | contains | in
    """

    __tablename__ = "endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    name: Mapped[str] = mapped_column(String(160))
    description: Mapped[str] = mapped_column(Text, default="")
    mapping_id: Mapped[int] = mapped_column(ForeignKey("mappings.id"))
    params: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    public: Mapped[bool] = mapped_column(Boolean, default=False)
    page_size: Mapped[int] = mapped_column(Integer, default=100)
    formats: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["json", "csv", "xlsx"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    pinned_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)


class ApiKey(Base):
    """Consumer key. Only the SHA-256 hash is stored; the raw key is shown once."""

    __tablename__ = "api_keys"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    prefix: Mapped[str] = mapped_column(String(16))
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    endpoints: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["*"])
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class Run(Base):
    """Audit record for ingests, extracts and publishes."""

    __tablename__ = "runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # ingest | extract | publish
    subject: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | warning | failed
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    message: Mapped[str] = mapped_column(Text, default="")


# ---------------------------------------------------------------- authentication


class User(Base):
    """Studio account. Passwords are stored as salted scrypt hashes; admins create accounts."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(80), unique=True)  # stored lower-case
    full_name: Mapped[str] = mapped_column(String(160), default="")
    email: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(20), default="viewer")  # admin | designer | viewer
    password_hash: Mapped[str] = mapped_column(Text)
    must_change_password: Mapped[bool] = mapped_column(Boolean, default=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    failed_attempts: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    last_login_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    password_changed_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_by: Mapped[str] = mapped_column(String(80), default="")


class UserSession(Base):
    """Signed-in session. Only a SHA-256 of the token is stored."""

    __tablename__ = "user_sessions"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    remember: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    ip: Mapped[str] = mapped_column(String(64), default="")
    user_agent: Mapped[str] = mapped_column(String(300), default="")
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)


class AuditLog(Base):
    """Who did what, when: sign-ins, account changes and changes to connections, mappings, endpoints, keys."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    username: Mapped[str] = mapped_column(String(80), default="")
    action: Mapped[str] = mapped_column(String(60))
    target: Mapped[str] = mapped_column(String(200), default="")
    detail: Mapped[str] = mapped_column(Text, default="")
    ip: Mapped[str] = mapped_column(String(64), default="")


# ---------------------------------------------------------------- AI: models, prompts, usage


class ModelEndpoint(Base):
    """Shared model endpoint configured by an admin: local LLMs (Ollama, vLLM, LM Studio) or a company gateway."""

    __tablename__ = "model_endpoints"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120), unique=True)
    api_style: Mapped[str] = mapped_column(String(20), default="openai")  # openai | anthropic
    base_url: Mapped[str] = mapped_column(String(500))
    auth_header: Mapped[str] = mapped_column(String(60), default="Authorization")
    auth_scheme: Mapped[str] = mapped_column(String(20), default="Bearer")
    secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # encrypted {"api_key": ...}
    key_hint: Mapped[str] = mapped_column(String(8), default="")
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    default_model: Mapped[str] = mapped_column(String(200), default="")
    network: Mapped[str] = mapped_column(String(10), default="internal")  # internal | external
    allowed_roles: Mapped[list[str]] = mapped_column(JSON, default=lambda: ["admin", "designer"])
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    timeout_s: Mapped[int] = mapped_column(Integer, default=120)
    # TLS: server verification (system | custom | off), custom CA, client certificate for mutual TLS
    tls_verify: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="system")
    tls_check_hostname: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=True)
    ca_pem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # public certificates, stored as-is
    client_cert_pem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    client_key_secret: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # Fernet-encrypted {"key_pem"}
    tls_info: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)  # subjects and expiry dates
    # Keys: "shared" = one company key (secret); "per_user" = each user brings their own issued key (secret, if set,
    # is only a discovery key for tests and health checks); "none" = no key.
    key_mode: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="shared")
    # Model catalog: [{id, label, description, enabled, roles}]; approval "open" = every discovered model is usable
    # unless disabled, "approved" = only models enabled in the catalog.
    approval: Mapped[Optional[str]] = mapped_column(String(10), nullable=True, default="open")
    catalog: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    managed: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True, default=False)  # from the config file
    health: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)  # last check
    health_history: Mapped[Optional[list[dict[str, Any]]]] = mapped_column(JSON, nullable=True)
    extra_headers: Mapped[Optional[dict[str, str]]] = mapped_column(JSON, nullable=True)  # static, non-secret
    # Endpoints with the same key group share users' issued keys (e.g. one gateway host, several model paths)
    key_group: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    source_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # AiConfigSource that created it
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserCredential(Base):
    """A user's own LLM API key (OpenAI, Anthropic, Gemini, ...). Only the owner can use it; nobody can read it."""

    __tablename__ = "user_credentials"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(String(120))
    provider: Mapped[str] = mapped_column(String(40))  # preset key, e.g. openai, anthropic, gemini, custom
    api_style: Mapped[str] = mapped_column(String(20), default="openai")
    base_url: Mapped[str] = mapped_column(String(500))
    auth_header: Mapped[str] = mapped_column(String(60), default="Authorization")
    auth_scheme: Mapped[str] = mapped_column(String(20), default="Bearer")
    secret: Mapped[str] = mapped_column(Text)  # encrypted {"api_key": ...}
    key_hint: Mapped[str] = mapped_column(String(8), default="")
    models: Mapped[list[str]] = mapped_column(JSON, default=list)
    default_model: Mapped[str] = mapped_column(String(200), default="")
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class PromptTemplate(Base):
    """Reusable prompt: system + user template with declared variables. The row holds the working draft;
    publishing snapshots it into PromptVersion."""

    __tablename__ = "prompt_templates"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    system: Mapped[str] = mapped_column(Text, default="")
    user: Mapped[str] = mapped_column(Text, default="")
    variables: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # [{name, default, description}]
    model_ref: Mapped[str] = mapped_column(String(300), default="")
    temperature: Mapped[Optional[float]] = mapped_column(nullable=True)
    max_tokens: Mapped[int] = mapped_column(Integer, default=1024)
    json_mode: Mapped[bool] = mapped_column(Boolean, default=False)
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    has_draft_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    owner: Mapped[str] = mapped_column(String(80), default="")
    updated_by: Mapped[str] = mapped_column(String(80), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class PromptVersion(Base):
    __tablename__ = "prompt_versions"

    id: Mapped[int] = mapped_column(primary_key=True)
    template_id: Mapped[int] = mapped_column(ForeignKey("prompt_templates.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    note: Mapped[str] = mapped_column(Text, default="")
    created_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LlmCall(Base):
    """Usage ledger: one row per model call."""

    __tablename__ = "llm_calls"

    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    user_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    username: Mapped[str] = mapped_column(String(80), default="")
    purpose: Mapped[str] = mapped_column(String(40), default="playground")  # playground | prompt_test | automap | workflow
    model_ref: Mapped[str] = mapped_column(String(300))
    model: Mapped[str] = mapped_column(String(200), default="")
    route: Mapped[str] = mapped_column(String(160), default="")  # endpoint or key name
    network: Mapped[str] = mapped_column(String(10), default="")
    prompt_template: Mapped[str] = mapped_column(String(200), default="")
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(20), default="ok")  # ok | error | blocked
    error: Mapped[str] = mapped_column(Text, default="")
    request_text: Mapped[str] = mapped_column(Text, default="")
    response_text: Mapped[str] = mapped_column(Text, default="")


class Workflow(Base):
    """An AI workflow: a graph of nodes (inputs, LLM, logic, outputs) with guardrails.

    spec = {"nodes": [{id, type, label, x, y, config}], "edges": [{from, to, port}], "limits": {...}}
    The draft lives in spec; publishing freezes it into published_spec (what API and scheduled runs use).
    """

    __tablename__ = "workflows"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160), unique=True)
    slug: Mapped[str] = mapped_column(String(80), unique=True)
    description: Mapped[str] = mapped_column(Text, default="")
    spec: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    published_spec: Mapped[Optional[dict[str, Any]]] = mapped_column(JSON, nullable=True)
    published_version: Mapped[int] = mapped_column(Integer, default=0)
    has_draft_changes: Mapped[bool] = mapped_column(Boolean, default=True)
    owner: Mapped[str] = mapped_column(String(80), default="")
    updated_by: Mapped[str] = mapped_column(String(80), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class WorkflowRun(Base):
    __tablename__ = "workflow_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    workflow_id: Mapped[int] = mapped_column(ForeignKey("workflows.id", ondelete="CASCADE"))
    version: Mapped[int] = mapped_column(Integer, default=0)  # 0 = draft
    trigger: Mapped[str] = mapped_column(String(20), default="manual")  # manual | test | api
    status: Mapped[str] = mapped_column(String(20), default="running")  # running | ok | warning | failed | blocked
    started_by: Mapped[str] = mapped_column(String(80), default="")
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    finished_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    input: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    estimate: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    rows_flagged: Mapped[int] = mapped_column(Integer, default=0)
    rows_rejected: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    guardrail_events: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)  # {"pii masked": 12, ...}
    output_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # parquet of the final rows
    review_path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)  # flagged + rejected rows
    output_source_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    error: Mapped[str] = mapped_column(Text, default="")


class NodeRun(Base):
    __tablename__ = "node_runs"

    id: Mapped[int] = mapped_column(primary_key=True)
    run_id: Mapped[int] = mapped_column(ForeignKey("workflow_runs.id", ondelete="CASCADE"))
    node_id: Mapped[str] = mapped_column(String(40))
    node_type: Mapped[str] = mapped_column(String(30))
    label: Mapped[str] = mapped_column(String(160), default="")
    status: Mapped[str] = mapped_column(String(20), default="ok")
    rows_in: Mapped[int] = mapped_column(Integer, default=0)
    rows_out: Mapped[int] = mapped_column(Integer, default=0)
    flagged: Mapped[int] = mapped_column(Integer, default=0)
    rejected: Mapped[int] = mapped_column(Integer, default=0)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens: Mapped[int] = mapped_column(Integer, default=0)
    ms: Mapped[int] = mapped_column(Integer, default=0)
    events: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    samples: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # first few prompts/replies (masked)
    error: Mapped[str] = mapped_column(Text, default="")


class UserEndpointKey(Base):
    """A user's own key for a company endpoint in per-user key mode (keys issued individually by the company)."""

    __tablename__ = "user_endpoint_keys"
    __table_args__ = (UniqueConstraint("user_id", "endpoint_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    endpoint_id: Mapped[int] = mapped_column(ForeignKey("model_endpoints.id", ondelete="CASCADE"))
    secret: Mapped[str] = mapped_column(Text)  # encrypted {"api_key": ...}
    key_hint: Mapped[str] = mapped_column(String(8), default="")
    last_test_ok: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    last_test_message: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_used_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class AiSetting(Base):
    """Small key/value settings for AI, e.g. "defaults" = {playground, automap, workflow: model ref}."""

    __tablename__ = "ai_settings"

    key: Mapped[str] = mapped_column(String(60), primary_key=True)
    value: Mapped[Any] = mapped_column(JSON)
    updated_by: Mapped[str] = mapped_column(String(80), default="")
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)


class AiConfigSource(Base):
    """An uploaded company LLM configuration file (Continue config.yaml or DataBridge format), kept as the custom
    source of the endpoints it created. Real keys are redacted from the kept copy."""

    __tablename__ = "ai_config_sources"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(160))
    format: Mapped[str] = mapped_column(String(20))  # continue | databridge
    file_name: Mapped[str] = mapped_column(String(255), default="")
    original: Mapped[str] = mapped_column(Text)  # the uploaded file, keys redacted
    version: Mapped[str] = mapped_column(String(60), default="")
    revision: Mapped[int] = mapped_column(Integer, default=1)
    options: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)  # key_mode, network, allowed_roles
    ca_pems: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)  # {file name: PEM} (public certificates)
    standard: Mapped[str] = mapped_column(Text, default="")  # the mapped DataBridge configuration (YAML)
    mapping: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)  # rows shown in the preview
    notes: Mapped[list[str]] = mapped_column(JSON, default=list)  # repairs and warnings
    endpoint_ids: Mapped[list[int]] = mapped_column(JSON, default=list)
    uploaded_by: Mapped[str] = mapped_column(String(80), default="")
    uploaded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

