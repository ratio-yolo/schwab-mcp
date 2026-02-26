# Deploying Schwab MCP to Google Cloud Run

This guide covers deploying two Cloud Run services:

| Service | Purpose | Access |
|---------|---------|--------|
| `schwab-mcp` | Remote MCP server (Streamable HTTP + OAuth) | Public (OAuth-protected) |
| `schwab-mcp-admin` | Admin UI for Schwab token re-authentication | Public (URL-only) |

Both services connect to a shared Cloud SQL Postgres database. Secrets are stored in Google Secret Manager.

## Prerequisites

- GCP project with billing enabled
- `gcloud` CLI installed and authenticated (`gcloud auth login`)
- Docker or Podman
- A Schwab Developer Portal app with API credentials
- A custom domain for the admin service (e.g., `admin.example.com`), or use the default Cloud Run URL

```bash
# Verify gcloud is configured
gcloud config get-value project
gcloud auth list
```

## 1. Cloud SQL Setup

Create a Postgres instance and database:

```bash
PROJECT_ID=$(gcloud config get-value project)
REGION=us-west1
INSTANCE_NAME=schwab-mcp-db

# Create the instance
gcloud sql instances create "$INSTANCE_NAME" \
    --database-version=POSTGRES_15 \
    --tier=db-f1-micro \
    --region="$REGION" \
    --project="$PROJECT_ID"

# Create the database
gcloud sql databases create schwab_data \
    --instance="$INSTANCE_NAME" \
    --project="$PROJECT_ID"

# Create the user (save this password — you'll add it to Secret Manager)
gcloud sql users create agent_user \
    --instance="$INSTANCE_NAME" \
    --password="YOUR_SECURE_PASSWORD" \
    --project="$PROJECT_ID"
```

Note the instance connection name — you'll need it for deployment:

```bash
gcloud sql instances describe "$INSTANCE_NAME" \
    --format='value(connectionName)' \
    --project="$PROJECT_ID"
# Output format: project-id:us-west1:schwab-mcp-db
```

The schema (`sql/001_create_schwab_data.sql`) is auto-applied when the services start. To apply manually:

```bash
gcloud sql connect "$INSTANCE_NAME" --database=schwab_data --user=agent_user
# Then paste the contents of sql/001_create_schwab_data.sql
```

## 2. Secret Manager Setup

Create secrets for all sensitive values:

```bash
# Schwab API credentials (from Schwab Developer Portal)
echo -n "YOUR_SCHWAB_CLIENT_ID" | \
    gcloud secrets create schwab-client-id --data-file=- --project="$PROJECT_ID"

echo -n "YOUR_SCHWAB_CLIENT_SECRET" | \
    gcloud secrets create schwab-client-secret --data-file=- --project="$PROJECT_ID"

# Database password (same password you set in Cloud SQL)
echo -n "YOUR_DB_PASSWORD" | \
    gcloud secrets create schwab-db-password --data-file=- --project="$PROJECT_ID"

# OAuth secret for claude.ai MCP authentication
# Generate a random value:
echo -n "$(openssl rand -hex 32)" | \
    gcloud secrets create schwab-mcp-oauth-secret --data-file=- --project="$PROJECT_ID"
```

To update an existing secret:

```bash
echo -n "NEW_VALUE" | \
    gcloud secrets versions add schwab-client-id --data-file=- --project="$PROJECT_ID"
```

## 3. IAM & Service Account Configuration

The default Cloud Run service account needs permissions to connect to Cloud SQL and read secrets:

```bash
SA_EMAIL=$(gcloud iam service-accounts list \
    --filter="displayName:Default compute service account" \
    --format='value(email)' \
    --project="$PROJECT_ID")

# Cloud SQL access
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/cloudsql.client"

# Secret Manager access
gcloud projects add-iam-policy-binding "$PROJECT_ID" \
    --member="serviceAccount:$SA_EMAIL" \
    --role="roles/secretmanager.secretAccessor"
```

### Admin Service Access

The admin service is deployed with `--no-allow-unauthenticated`. Access is controlled by Cloud Run IAM.

The Schwab OAuth callback redirects the browser to `/datareceived` on the admin service, which requires public access to work. The `schwab-auth.sh` script handles this by temporarily granting public access for the duration of the auth flow, then revoking it automatically (see [§7](#7-token-setuprefresh)).

## 4. Deploy Services

The `deploy.sh` script handles building and deploying both services. It runs `gcloud run deploy --source .` which uploads the source and builds container images in Cloud Build using `Dockerfile.mcp` (MCP server) and `Dockerfile.admin` (admin service). Both Dockerfiles use a multi-stage build: a builder stage compiles a wheel and exports dependencies, then a slim runtime stage installs only what's needed.

Key things `deploy.sh` does:

- Wires Secret Manager secrets into environment variables on each service
- Attaches the Cloud SQL instance via `--add-cloudsql-instances`
- For the MCP service: deploys once, reads back the assigned URL, then updates `SERVER_URL` (needed for OAuth redirects)
- For the admin service: sets `SCHWAB_CALLBACK_URL` using `ADMIN_DOMAIN` (if set) or the Cloud Run-assigned URL

### Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `PROJECT_ID` | GCP project ID | From `gcloud config` |
| `REGION` | Cloud Run region | `us-west1` |
| `DB_INSTANCE` | Cloud SQL connection name | *required* |
| `DB_NAME` | Database name | `schwab_data` |
| `DB_USER` | Database user | `agent_user` |

Secret names (referencing Secret Manager, not the values):

| Variable | Default |
|----------|---------|
| `SCHWAB_CLIENT_ID_SECRET` | `schwab-client-id` |
| `SCHWAB_CLIENT_SECRET_SECRET` | `schwab-client-secret` |
| `DB_PASSWORD_SECRET` | `schwab-db-password` |
| `MCP_OAUTH_SECRET` | `schwab-mcp-oauth-secret` |
| `ADMIN_DOMAIN` | *(none — uses Cloud Run URL)* |

Set `ADMIN_DOMAIN` to your custom domain (e.g., `admin.example.com`) if you configured one in [§5](#5-domain-mapping-optional). When unset, the Schwab callback URL defaults to the Cloud Run-assigned URL.

### Deploy Both Services

```bash
DB_INSTANCE="your-project:us-west1:schwab-mcp-db" ./deploy.sh

# With a custom admin domain:
ADMIN_DOMAIN="admin.example.com" \
  DB_INSTANCE="your-project:us-west1:schwab-mcp-db" ./deploy.sh
```

### Deploy Individually

```bash
DB_INSTANCE="your-project:us-west1:schwab-mcp-db" ./deploy.sh --admin-only
DB_INSTANCE="your-project:us-west1:schwab-mcp-db" ./deploy.sh --mcp-only
```

### MCP Server Two-Step Deploy

The MCP server needs its own URL set as `SERVER_URL` for OAuth to work. The deploy script handles this automatically:

1. First deploy creates the service and assigns a URL
2. Script reads the URL back with `gcloud run services describe`
3. Updates the service with `SERVER_URL` set to the assigned URL

On subsequent deploys this is a no-op update since the URL doesn't change.

### Trading and Discord Approval

The remote MCP server supports the same Discord approval workflow as the local server. Without Discord configured, trading tools are **not registered** — the server is read-only by default.

To enable trading with Discord approval, add these secrets and environment variables to the MCP service:

```bash
# Store Discord secrets
echo -n "YOUR_DISCORD_BOT_TOKEN" | \
    gcloud secrets create schwab-mcp-discord-token --data-file=- --project="$PROJECT_ID"

# Update the MCP service with Discord config
gcloud run services update schwab-mcp \
    --region="$REGION" \
    --project="$PROJECT_ID" \
    --set-secrets "SCHWAB_MCP_DISCORD_TOKEN=schwab-mcp-discord-token:latest" \
    --update-env-vars "SCHWAB_MCP_DISCORD_CHANNEL_ID=YOUR_CHANNEL_ID,SCHWAB_MCP_DISCORD_APPROVERS=YOUR_USER_ID"
```

See [Discord Setup Guide](discord-setup.md) for creating the bot and obtaining these values.

> **⚠️ Warning:** Setting `JESUS_TAKE_THE_WHEEL=true` bypasses all approval checks. Do not use in production unless you fully understand the risks.

## 5. Domain Mapping (Optional)

> **Note:** A custom domain is optional. If you skip this step, use the Cloud Run-assigned URL as your Schwab callback URL instead. The `deploy.sh` script handles this automatically when `ADMIN_DOMAIN` is not set.

Replace `admin.example.com` below with your own domain.

Map it to the admin Cloud Run service:

```bash
ADMIN_DOMAIN="admin.example.com"

gcloud beta run domain-mappings create \
    --service=schwab-mcp-admin \
    --domain="$ADMIN_DOMAIN" \
    --region="$REGION" \
    --project="$PROJECT_ID"
```

Then add a DNS CNAME record:

```
admin.example.com.  CNAME  ghs.googlehosted.com.
```

Cloud Run automatically provisions and renews the SSL certificate. Provisioning takes a few minutes after DNS propagates.

Check mapping status:

```bash
gcloud beta run domain-mappings describe \
    --domain="$ADMIN_DOMAIN" \
    --region="$REGION" \
    --project="$PROJECT_ID"
```

After the domain is mapped, update the admin service callback URL:

```bash
gcloud run services update schwab-mcp-admin \
    --region="$REGION" \
    --update-env-vars "SCHWAB_CALLBACK_URL=https://${ADMIN_DOMAIN}/datareceived" \
    --project="$PROJECT_ID"
```

## 6. Schwab Developer Portal Configuration

In the [Schwab Developer Portal](https://developer.schwab.com/), update your app's callback URL to match the callback URL printed by `deploy.sh`:

```
https://admin.example.com/datareceived
# or if using the default Cloud Run URL:
https://<ADMIN_SERVICE_URL>/datareceived
```

> **Note:** Schwab callback URL changes typically only take effect after market close. Plan accordingly.

## 7. Token Setup/Refresh

After deployment (and every ~7 days when the Schwab refresh token expires), run:

```bash
./schwab-auth.sh
```

This script:
1. Temporarily grants public access to the admin service
2. Opens the admin dashboard in your browser
3. You click **"Start Schwab Auth"** and log in to Schwab
4. After redirect to `/datareceived`, the token is written to Postgres
5. The script detects the token and revokes public access automatically

The script has a 5-minute timeout — if auth isn't completed, access is revoked on exit (including Ctrl+C).

## 8. Connecting claude.ai

Once the MCP server is deployed and has a valid token:

1. In claude.ai, go to **Settings → Integrations → Add Integration**
2. Add a remote MCP server with URL:
   ```
   https://<MCP_SERVICE_URL>/mcp
   ```
   The service URL is printed by `deploy.sh`, or retrieve it:
   ```bash
   gcloud run services describe schwab-mcp \
       --region="$REGION" \
       --format='value(status.url)' \
       --project="$PROJECT_ID"
   ```
3. claude.ai handles OAuth registration automatically
4. You'll be prompted to approve access on a consent page

## Troubleshooting

### Check Token Status

```bash
# Admin service status endpoint
curl -H "Authorization: Bearer $(gcloud auth print-identity-token)" \
    https://<ADMIN_DOMAIN>/status

# MCP server token status (public)
curl https://<MCP_SERVICE_URL>/token-status
```

### Proxy Admin Service Locally

Useful when you can't access the admin UI directly:

```bash
gcloud run services proxy schwab-mcp-admin \
    --region="$REGION" \
    --project="$PROJECT_ID"
# Then open http://localhost:8080
```

### View Logs

```bash
# Admin service logs
gcloud run services logs read schwab-mcp-admin \
    --region="$REGION" --project="$PROJECT_ID" --limit=50

# MCP server logs
gcloud run services logs read schwab-mcp \
    --region="$REGION" --project="$PROJECT_ID" --limit=50
```

### Common Issues

| Problem | Cause | Fix |
|---------|-------|-----|
| MCP tools return errors about expired token | Schwab refresh token expired (>7 days) | Re-auth via admin dashboard |
| `Cloud SQL connection failed` | Missing `roles/cloudsql.client` or wrong instance name | Verify IAM bindings and `DB_INSTANCE` value |
| `403 Forbidden` on admin callback | Admin service requires IAM auth | Use `./schwab-auth.sh` to temporarily open access (see §7) |
| `SCHWAB_CALLBACK_URL is required` | Missing env var on admin service | Update with `gcloud run services update` |
| Domain mapping stuck on "pending" | DNS not propagated | Verify CNAME record points to `ghs.googlehosted.com` |
| OAuth callback fails after portal update | Schwab hasn't activated the new callback URL | Wait until after market close |

## Cost Estimates

This deployment uses minimal resources. Approximate monthly costs (as of 2025):

| Resource | Tier | Estimated Cost |
|----------|------|----------------|
| Cloud SQL (Postgres) | `db-f1-micro` | ~$7–10/mo |
| Cloud Run (MCP server) | 512 MiB, max 1 instance | Free tier likely covers it; ~$0–5/mo |
| Cloud Run (Admin service) | 256 MiB, max 1 instance | Free tier likely covers it; ~$0–2/mo |
| Secret Manager | 4 secrets | Free (< 10K access ops/mo) |
| Cloud Build | Source builds on deploy | Free tier (120 min/day) |

Cloud Run charges only for request processing time, not idle time. With light usage the total is roughly **$7–15/month**, dominated by Cloud SQL.

> **Tip:** For lower costs, consider stopping the Cloud SQL instance when not in use: `gcloud sql instances patch schwab-mcp-db --activation-policy=NEVER`.

## Teardown

To remove all deployed resources:

```bash
PROJECT_ID=$(gcloud config get-value project)
REGION=us-west1

# Delete Cloud Run services
gcloud run services delete schwab-mcp --region="$REGION" --project="$PROJECT_ID" --quiet
gcloud run services delete schwab-mcp-admin --region="$REGION" --project="$PROJECT_ID" --quiet

# Delete domain mapping (if configured)
# gcloud beta run domain-mappings delete --domain="admin.example.com" --region="$REGION" --project="$PROJECT_ID"

# Delete Cloud SQL instance (destroys all data!)
gcloud sql instances delete schwab-mcp-db --project="$PROJECT_ID" --quiet

# Delete secrets
gcloud secrets delete schwab-client-id --project="$PROJECT_ID" --quiet
gcloud secrets delete schwab-client-secret --project="$PROJECT_ID" --quiet
gcloud secrets delete schwab-db-password --project="$PROJECT_ID" --quiet
gcloud secrets delete schwab-mcp-oauth-secret --project="$PROJECT_ID" --quiet
```
