# SQL Lab Extractor

CLI Python untuk extraction SQL Lab terotorisasi secara streaming, bounded, dan dapat diaudit.

## Prasyarat

- Python 3.11+
- Akses resmi ke deployment Superset
- VPN/SSO/MFA sesuai kebijakan organisasi
- `camoufox`, `pyarrow`, dan `openpyxl`

Install dependency:

```powershell
python -m pip install -r requirements.txt
```

Camoufox membuka browser untuk login SSO/MFA. MFA tetap manual; tool tidak melewati MFA.

## Konfigurasi credential

Salin template:

```powershell
Copy-Item .env.example .env
```

Isi hanya pasangan berikut:

```text
SQL_LAB_USERNAME=nama-pengguna
SQL_LAB_PASSWORD=kata-sandi
```

Alternatif yang direkomendasikan untuk CI lokal adalah environment process:

```powershell
$env:SQL_LAB_USERNAME = "nama-pengguna"
$env:SQL_LAB_PASSWORD = "kata-sandi"
```

Jangan commit `.env`, cookie, token, HAR, browser profile, log, atau hasil extraction.
`.env.example` hanya template dan tidak berisi secret.

## Menjalankan extraction

Buat SQL dengan projection kolom eksplisit dan ordering deterministik:

```sql
SELECT assignment_id, created_at, value
FROM schema_name.table_name
ORDER BY assignment_id
```

Jalankan default:

```powershell
python -m sql_lab_extractor --sql-file query.sql
```

Output default: Parquet dan Excel di direktori run baru di `artifacts/`.

Opsi umum:

```powershell
python -m sql_lab_extractor `
  --sql-file query.sql `
  --page-size 1000 `
  --query-limit 100000 `
  --workers 2 `
  --final-format parquet `
  --final-format xlsx
```

Rate limiter internal menjaga jarak start request page untuk mematuhi rate limit server. `POST` yang timeout, 401, 403, atau 429 tidak di-replay otomatis.

## Resume

Resume hanya aman bila query dan snapshot count tetap sama. Gunakan run directory yang gagal:

```powershell
python -m sql_lab_extractor `
  --sql-file query.sql `
  --resume-run artifacts/run-YYYYMMDDTHHMMSSZ-xxxxxxxx
```

Resume menolak perubahan query, count, schema, database, page size, query limit, atau ordering contract. Jika source berubah, mulai run baru.

## Artifact dan audit

Setiap run menyimpan:

- `query.sql`
- `manifest.json`
- `progress.jsonl`
- `run.log`
- atomic page CSV
- failure diagnostics yang sudah direduksi
- `result.parquet` dan/atau `result.xlsx`

`artifacts/` di-ignore Git. Ignore bukan security boundary: batasi permission direktori dan jangan sinkronkan artifact sensitif ke layanan yang tidak disetujui.

## Keamanan data

- Cookie, CSRF, authorization header, token, credential, request body, dan response rows tidak disimpan di diagnostic HTTP.
- Error body dibatasi dan direduksi.
- SQL tersimpan di artifact run lokal karena diperlukan untuk resume/audit; lindungi direktori tersebut.
- Jangan gunakan tool ini tanpa otorisasi query dan data owner yang sesuai.

## Pengujian

```powershell
python -m unittest discover -s tests
python -m compileall -q sql_lab_extractor tests
```
