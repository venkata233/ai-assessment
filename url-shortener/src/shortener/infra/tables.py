"""SQLAlchemy Core table definitions. They mirror migration 0001 so `alembic check` can detect drift."""

import sqlalchemy as sa

metadata = sa.MetaData()

urls = sa.Table(
    "urls",
    metadata,
    sa.Column("id", sa.BigInteger(), sa.Identity(always=True), nullable=False),
    sa.Column("code", sa.String(32, collation="C"), nullable=False),
    sa.Column("target_url", sa.String(2048), nullable=False),
    sa.Column("owner_key_id", sa.String(64), nullable=False),
    sa.Column("is_custom_alias", sa.Boolean(), server_default=sa.false(), nullable=False),
    sa.Column("click_count", sa.BigInteger(), server_default="0", nullable=False),
    sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
    sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    sa.PrimaryKeyConstraint("id", name="pk_urls"),
    sa.UniqueConstraint("code", name="uq_urls_code"),
    sa.CheckConstraint("code ~ '^[A-Za-z0-9_-]{3,32}$'", name="ck_urls_code_format"),
    sa.CheckConstraint("target_url ~* '^https?://'", name="ck_urls_target_scheme"),
    sa.CheckConstraint("expires_at IS NULL OR expires_at > created_at", name="ck_urls_expiry_after_creation"),
    sa.CheckConstraint("click_count >= 0", name="ck_urls_click_count_non_negative"),
)

clicks = sa.Table(
    "clicks",
    metadata,
    sa.Column("event_id", sa.String(40), nullable=False),
    sa.Column("url_id", sa.BigInteger(), nullable=False),
    sa.Column("clicked_at", sa.DateTime(timezone=True), nullable=False),
    sa.Column("referrer", sa.String(255), nullable=True),
    sa.Column("user_agent", sa.String(255), nullable=True),
    sa.Column("ua_family", sa.String(32), server_default="other", nullable=False),
    sa.Column("country", sa.CHAR(2), nullable=True),
    sa.Column("visitor_hash", sa.CHAR(16), nullable=True),
    sa.Column("ingested_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    sa.PrimaryKeyConstraint("event_id", name="pk_clicks"),
    sa.ForeignKeyConstraint(["url_id"], ["urls.id"], name="fk_clicks_url_id_urls", ondelete="RESTRICT"),
    sa.CheckConstraint("country IS NULL OR country ~ '^[A-Z]{2}$'", name="ck_clicks_country_format"),
    sa.CheckConstraint(
        r"referrer IS NULL OR referrer ~ '^https?://[^/?#@[:space:]]+$'", name="ck_clicks_referrer_origin"
    ),
    sa.Index("ix_clicks_url_id_clicked_at", "url_id", sa.text("clicked_at DESC"), sa.text("event_id DESC")),
)
