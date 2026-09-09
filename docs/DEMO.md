# Two-day demo deployment

App Runner rather than ECS+ALB: the load balancer alone is ~$16/month, which
is most of the cost for something that will be torn down in 48 hours. App
Runner gives HTTPS, scaling and a public URL with no balancer to pay for.

**Roughly $1.20 for two days.**

The trade-off is no persistent volume: generated images and the database are
container-local, so a restart clears them and the user scans again. Acceptable
for a short demo; mount a volume or sync to S3 if results need to survive.

## Deploy

```bash
cd ~/Desktop/At-scale/curbside
export GEMINI_API_KEY=...          # goes to Secrets Manager, not the image
./deploy-apprunner.sh
```

Prints the API URL when it is up. Takes 3–5 minutes on a first run.

## Front end

```bash
cd web
VITE_API_BASE=https://<api-url> npm run build
npx vercel deploy --prod dist
```

Free, and gives John a single link.

## Tear down

```bash
./destroy-apprunner.sh
```

Removes the service, image repository, secret and roles. Billing stops when
the service finishes deleting.

## Live from a cold start

Nothing is pre-generated. Type an address and the full pipeline runs: find the
neighbours, fetch imagery, qualify driveways, render, QC, build postcards.
About 50-70 seconds for a six-home block, roughly 20c of model calls.

`CURBSIDE_BUDGET=20.00` is a ceiling, not a limit anyone should reach - around
100 scans. It exists so a bug or a widely-shared link cannot drain the key.

### Why one instance stays warm

A scan runs as a background task, so `POST /scan` returns immediately and the
UI polls for progress. App Runner pauses idle instances by default, which
would strand a scan mid-render, so the deployment pins `--min-size 1`.

That also avoids a cold start on the first request - a two-minute wait would
read as a broken app.

## Costs

| | Two days |
|---|---|
| App Runner 0.25 vCPU / 0.5 GB | ~$1.20 |
| ECR storage | ~$0.01 |
| Secrets Manager | ~$0.03 |
| **Total** | **~$1.25** |

Against $200 of credit, negligible — but tear it down anyway, since an idle
service still bills for provisioned memory.

## If something fails

| Symptom | Cause |
|---|---|
| `CREATE_FAILED` | Usually the ECR access role. Check the App Runner console's event log. |
| Health check failing | The container must listen on `$PORT`; the Dockerfile handles this. |
| Images 404 | `var/` was excluded from the build. Check `.dockerignore`. |
| Scans empty | `var/seeded_scans.json` did not ship. |
| CORS errors | Set `CURBSIDE_CORS_ORIGINS` to the Vercel domain. |
