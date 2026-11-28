# DLX Tray Cloud AI proxy

The service provides translation and document term extraction for authorized team members.
It runs separately from the Windows desktop app.

## Run locally

From the repository root, build and start the Linux image with Docker:

```powershell
docker build -f cloud/Dockerfile -t dlx-cloud .
docker run --rm -p 127.0.0.1:8080:8080 -e PORT=8080 dlx-cloud
```

In a second terminal, `Invoke-RestMethod http://127.0.0.1:8080/healthz` should return
`{"status":"ok"}`. The health endpoint checks the process. The authenticated routes need the
credentials below and return 503 when either secret is absent. For a credential-free test of the
whole API, run `uv run --locked pytest tests/cloud --cov=dlx_cloud --cov-fail-under=85` from the
repository root.

## Configure the service

| Environment variable | Value |
| --- | --- |
| `DLX_CLOUD_TOKEN_PEPPER` | A random secret of at least 32 characters, shared by the service and token admin CLI. Changing it invalidates existing tokens. |
| `DLX_CLOUD_GEMINI_API_KEY` | API key for the Gemini Developer API. The key must have access to the configured models. |
| `DLX_CLOUD_FIRESTORE_PROJECT` | Google Cloud project ID. When omitted, Application Default Credentials supply the project. |
| `DLX_CLOUD_FIRESTORE_DATABASE` | Firestore Native mode database ID. Defaults to `(default)`. |
| `DLX_CLOUD_MODEL_STANDARD` | Defaults to `gemini-4-hydrogen`. |
| `DLX_CLOUD_MODEL_DEEP` | Defaults to `gemini-4-neon`. |
| `DLX_CLOUD_CACHE_TTL_DAYS` | Defaults to 60 days. |
| `DLX_CLOUD_MAX_BODY_SIZE` | Maximum POST body size in bytes. Defaults to 524288. |

Use Cloud Run's service identity for Firestore access via Application Default Credentials and
supply both secrets from Secret Manager. The image listens on the injected `PORT` and runs as a
non-root user. Enable a Firestore TTL policy for the `expires_at` field of the
`translation_cache` collection group. The proxy treats expired documents as misses while
Firestore deletes them asynchronously.

## Administer tokens

From the public repository root on PowerShell, use an identity with Firestore access. Configure
the Firestore environment and load the same pepper securely into the environment before running
the commands below. Token administration needs no Gemini API key.

```powershell
$env:PYTHONPATH = "cloud"
uv run --locked python -m dlx_cloud.cli create "Seminar reader" --allow-deep-tier
uv run --locked python -m dlx_cloud.cli list
uv run --locked python -m dlx_cloud.cli usage <token_hash>
uv run --locked python -m dlx_cloud.cli rotate <token_hash>
uv run --locked python -m dlx_cloud.cli revoke <token_hash>
```

Use the hash returned by `list` in place of `<token_hash>`. The `create` command accepts
`--daily-limit`, `--monthly-limit` and `--expires-at` with a timezone-aware ISO 8601 timestamp.
`create` and `rotate` display the new bearer token once. Avoid recording their output in shell
transcripts or logs. Rotation revokes the old token atomically and carries its quota limits,
current usage, and expiration to the replacement.

## API

Send the personal token in the `Authorization: Bearer …` header of `/v1` requests.

- `GET /v1/me` returns token permissions and remaining daily and monthly UTF-16 quotas.
- `POST /v1/translate` returns one result per input segment in input order. The
  `X-Cloud-Cache` header is `hit`, `mixed`, or `miss`.
- `POST /v1/extract-terms` returns candidate glossary terms. It requires deep-tier permission.

Both POST endpoints return `X-Quota-Daily-Remaining` and `X-Quota-Monthly-Remaining`
on success. Quotas reset at UTC day and month boundaries. Shared cache hits cost zero quota.
Each Gemini dispatch, including the single retry of a malformed response, reserves its source
UTF-16 units before the call. Requests rejected before dispatch cost zero quota.

Gemini Interactions use `store=False`. Shared Firestore documents contain translated output,
notes, language and model-tier metadata, and timestamps under a SHA-256 key. The cache omits
caller identity and source fields. Results containing a complete verbatim copy of their input
segment bypass the cache.

See the [Gemini Interactions API](https://aistudio.google.com/docs/interactions-overview) and the
[Firestore TTL guide](https://docs.cloud.google.com/firestore/native/docs/ttl).
