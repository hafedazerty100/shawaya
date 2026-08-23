# Database Migration & Setup Summary: Neon to Supabase (Session Pooler)

**Date**: August 23, 2026  
**Project**: Shawaya POS (`shawaya_shop`)

---

## 1. Target Database Details
- **Provider**: Supabase PostgreSQL (Session Pooler)
- **Connection String**:
  `postgresql://postgres.neouimhepbutsatyuulx:A.p.p.le.99.3.4%40gmail.com@aws-1-eu-west-1.pooler.supabase.com:5432/postgres`

---

## 2. Changes Implemented

### Configuration Files Updated
- `.env`: Updated `DATABASE_URL`, set `DB_REPLICATION_INTERVAL=3600` and `SYNC_INTERVAL=3600` (hourly sync).
- `.env.production`: Updated production environment file for Render deployment.
- `db_urls.json`: Updated active database array to target Supabase.
- `config.py` & `app.py`: Set default background replication timer to 1 hour (3600 seconds).

### Code & Schema Adjustments
- `models.py`: Increased `SyncLog.direction` column length from `VARCHAR(10)` to `VARCHAR(50)` to support sync tag strings like `pull_orders`.
- `migrate_to_supabase.py`: Built migration script to handle full transfer of PostgreSQL tables, sequences, foreign key constraints, and raw binary image blobs (`image_data` BYTEA + `image_mime`).

---

## 3. Migration Verification Report

| Entity | Record Count | Notes / Status |
| :--- | :--- | :--- |
| **Admin Users** (`admin_users`) | 1 | Credentials & permissions preserved |
| **Categories** (`categories`) | 4 | Order & names intact |
| **Products** (`products`) | 34 | **34 / 34 binary image blobs (`image_data`) copied** |
| **Orders** (`orders`) | 2 | UUIDs & order state preserved |
| **Order Items** (`order_items`) | 3 | Historical snapshot fields intact |
| **Serial Keys** (`serial_keys`) | 0 | Schema initialized |

### Test Results
- Automated pytest suite: **26 / 26 passed** cleanly (`pytest`).

---

## 4. Render Environment Variables (Copy & Paste)

```env
APP_MODE=server
DATABASE_URL=postgresql://postgres.neouimhepbutsatyuulx:A.p.p.le.99.3.4%40gmail.com@aws-1-eu-west-1.pooler.supabase.com:5432/postgres
SECRET_KEY=hafed13hafed
SYNC_API_KEY=hafed13hafed_sync_key_change_me
ADMIN_DEFAULT_USERNAME=admin
ADMIN_DEFAULT_PASSWORD=adminpassword123
DB_REPLICATION_INTERVAL=3600
SYNC_INTERVAL=3600
FLASK_DEBUG=False
```
