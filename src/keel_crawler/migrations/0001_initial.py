"""Initial keel-crawler migration — creates the HTTP-cache table (greenfield
default), or adopts a host's existing table when ``KEEL_CRAWLER["adopt_existing"]``
is True.

Two modes, selected at load time from settings so the same migration serves both a
fresh project and a host migrating off its own in-repo table:

* Greenfield (default, ``adopt_existing=False``): the ``CreateModel`` runs against
  the database, so a plain ``migrate`` builds the table.
* Adoption (``adopt_existing=True``): the operation is wrapped in
  ``SeparateDatabaseAndState`` with empty ``database_operations`` — Django records
  the model in migration STATE but emits no ``CREATE TABLE``, adopting the host's
  existing table (e.g. Revenika's populated ``core_crawl_http_cache``) untouched.

The table name and composite-index name are derived from
``KEEL_CRAWLER["http_cache_db_table"]`` — the same source ``CrawlHttpCache.Meta``
reads — so ``makemigrations --check`` stays clean for whatever table name the host
configures.
"""
from django.db import migrations, models

from keel_crawler.config import crawler_setting
from keel_crawler.models import CrawlHttpCache

_CACHE_TABLE = crawler_setting("http_cache_db_table")

# The composite index is unnamed in Meta, so Django derives its name from the
# db_table. Read that resolved name back off the model so the migration's explicit
# name matches whatever table the host configured — keeps makemigrations --check clean.
_HOST_EXP_INDEX_NAME = CrawlHttpCache._meta.indexes[0].name

_CREATE_CACHE = migrations.CreateModel(
    name="CrawlHttpCache",
    fields=[
        ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
        ("normalized_url", models.CharField(max_length=2048, unique=True)),
        ("hostname", models.CharField(db_index=True, max_length=253)),
        ("status_code", models.PositiveSmallIntegerField()),
        ("final_url", models.CharField(blank=True, default="", max_length=2048)),
        ("headers_json", models.JSONField(blank=True, default=dict)),
        ("body_text", models.TextField(blank=True, default="")),
        ("body_truncated", models.BooleanField(default=False)),
        ("sha256_hex", models.CharField(blank=True, default="", max_length=64)),
        ("fetched_at", models.DateTimeField(auto_now_add=True)),
        ("expires_at", models.DateTimeField(db_index=True)),
    ],
    options={
        "verbose_name": "Crawl HTTP cache entry",
        "verbose_name_plural": "Crawl HTTP cache entries",
        "db_table": _CACHE_TABLE,
        "indexes": [models.Index(fields=["hostname", "expires_at"], name=_HOST_EXP_INDEX_NAME)],
    },
)


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    if crawler_setting("adopt_existing"):
        operations = [
            migrations.SeparateDatabaseAndState(
                state_operations=[_CREATE_CACHE],
                database_operations=[],
            ),
        ]
    else:
        operations = [_CREATE_CACHE]
