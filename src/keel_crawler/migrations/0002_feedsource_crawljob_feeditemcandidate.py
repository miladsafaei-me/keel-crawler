"""Layer 3 (CrawlJob) + Layer 4 (FeedSource, FeedItemCandidate) tables.

``CrawlJob.db_table`` and its index names are read from the model so a host that
overrides ``KEEL_CRAWLER["crawl_job_db_table"]`` keeps ``makemigrations --check``
clean (same technique as 0001 for the HTTP cache). The RSS tables are fixed-name.
"""
import django.db.models.deletion
import uuid
from django.db import migrations, models

from keel_crawler.models import CrawlJob

_CRAWL_JOB_TABLE = CrawlJob._meta.db_table
_CRAWL_JOB_INDEX_NAMES = [ix.name for ix in CrawlJob._meta.indexes]


class Migration(migrations.Migration):

    dependencies = [
        ('keel_crawler', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='FeedSource',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('url', models.URLField(max_length=1000, unique=True)),
                ('name', models.CharField(blank=True, default='', max_length=200)),
                ('category', models.CharField(blank=True, default='', help_text='Free-text grouping tag.', max_length=100)),
                ('is_active', models.BooleanField(db_index=True, default=True)),
                ('weight', models.IntegerField(default=0, help_text='Source trust weight; used by the deterministic pre-filter.')),
                ('last_polled_at', models.DateTimeField(blank=True, null=True)),
                ('last_status', models.CharField(blank=True, default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Feed source',
                'verbose_name_plural': 'Feed sources',
                'db_table': 'keel_crawler_feed_source',
                'ordering': ['name', 'url'],
            },
        ),
        migrations.CreateModel(
            name='CrawlJob',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('batch_id', models.UUIDField(db_index=True, default=uuid.uuid4)),
                ('label', models.CharField(blank=True, default='', help_text="Free-form crawl kind, e.g. 'broker_review' or 'rss_article'.", max_length=100)),
                ('target_url', models.CharField(blank=True, default='', max_length=2048)),
                ('status', models.CharField(choices=[('pending', 'Pending'), ('fetching', 'Fetching'), ('parsing', 'Parsing'), ('succeeded', 'Succeeded'), ('skipped', 'Skipped'), ('failed', 'Failed')], db_index=True, default='pending', max_length=12)),
                ('input_snapshot', models.JSONField(blank=True, default=dict)),
                ('result_payload', models.JSONField(blank=True, default=dict)),
                ('error_text', models.TextField(blank=True, default='')),
                ('attempts', models.PositiveSmallIntegerField(default=0)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('started_at', models.DateTimeField(blank=True, null=True)),
                ('finished_at', models.DateTimeField(blank=True, null=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
            ],
            options={
                'verbose_name': 'Crawl job',
                'verbose_name_plural': 'Crawl jobs',
                'db_table': _CRAWL_JOB_TABLE,
                'ordering': ['-created_at'],
                'indexes': [models.Index(fields=['batch_id', 'status'], name=_CRAWL_JOB_INDEX_NAMES[0]), models.Index(fields=['label', 'status'], name=_CRAWL_JOB_INDEX_NAMES[1])],
            },
        ),
        migrations.CreateModel(
            name='FeedItemCandidate',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guid', models.CharField(max_length=800, unique=True)),
                ('title', models.CharField(blank=True, default='', max_length=500)),
                ('link', models.URLField(blank=True, default='', max_length=1000)),
                ('summary', models.TextField(blank=True, default='')),
                ('author', models.CharField(blank=True, default='', max_length=200)),
                ('published_at', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('raw', models.JSONField(blank=True, default=dict)),
                ('status', models.CharField(choices=[('fetched', 'Fetched (staged, not triaged)'), ('filtered_out', 'Dropped by deterministic pre-filter'), ('selected', 'Selected (worth processing)'), ('discarded', 'Discarded by triage'), ('processed', 'Handed off downstream')], db_index=True, default='fetched', max_length=14)),
                ('relevance_score', models.FloatField(blank=True, null=True)),
                ('triage_reason', models.TextField(blank=True, default='')),
                ('filter_reason', models.CharField(blank=True, default='', max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('updated_at', models.DateTimeField(auto_now=True)),
                ('source', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='items', to='keel_crawler.feedsource')),
            ],
            options={
                'verbose_name': 'Feed item candidate',
                'verbose_name_plural': 'Feed item candidates',
                'db_table': 'keel_crawler_feed_item',
                'ordering': ['-published_at', '-created_at'],
                'indexes': [models.Index(fields=['status', 'published_at'], name='keel_crawle_status_7e5599_idx')],
            },
        ),
    ]
