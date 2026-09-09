# Running Curbside locally

Two halves. The **API** does the work; the **dashboard** shows it to you. For a
full experience run both; for quick testing the CLI alone is enough.

## Setup — once

```bash
cd ~/Desktop/At-scale/curbside

pip install -e ".[api,dev]"        # backend
cd web && npm install && cd ..     # dashboard
```

Secrets live outside the repo:

```bash
# ~/.gemini_env  — the API key, never committed
echo 'GEMINI_API_KEY=AIza...' > ~/.gemini_env
chmod 600 ~/.gemini_env

# .env — local config, gitignored. Copy from the example.
cp .env.example .env
```

A physical return address is legally required on mailed pieces, so the
pipeline refuses to compose without one. `.env.example` has the fields.

## Check before you spend

```bash
python3 -m curbside.cli doctor
```

Reports what is configured and what would block a live send. Costs nothing.

## Three ways to run

### 1 · Tests — free, offline, no keys

```bash
python3 -m pytest tests/ -q        # 65 tests, ~2 seconds
```

Run this first. If it passes, the machine is intact.

### 2 · CLI — fastest for real work

```bash
set -a; source ~/.gemini_env; source .env; set +a

curbside run --limit 3       # ~$0.05  — process 3 addresses
curbside status              # states and spend
curbside review              # what is waiting for approval
curbside approve 3           # approve one lead
curbside mail                # dry run; writes receipts, sends nothing
```

Global flags go **before** the subcommand: `curbside --budget 1.00 run`.

### 3 · API + dashboard — the real experience

```bash
./run-local.sh               # starts both
```

- Dashboard: <http://localhost:5173>
- API docs: <http://localhost:8000/docs>

Or separately, in two terminals:

```bash
./run-local.sh api
./run-local.sh web
```

## What it costs

Nothing until a paid model is called. Free: geocoding, imagery, compositing,
QC arithmetic, postcard composition, tests, dry-run mail.

| Action | Cost |
|---|---|
| `pytest` | $0.00 |
| `doctor`, `status`, `review` | $0.00 |
| Qualify one address | $0.0009 |
| Render one qualified property | $0.067 |
| Segmentation + semantic QC | $0.0016 |
| **A qualified lead, end to end** | **~$0.07** |
| Dry-run mail | $0.00 |
| Real mail (Lob) | ~$0.75 |

`CURBSIDE_BUDGET` is a hard ceiling checked before every paid call. Start at
`1.00` while experimenting.

## Safe experimenting

```bash
curbside --limit 2 --budget 0.25 run    # two addresses, capped at 25 cents
curbside --db /tmp/scratch.db run       # throwaway database
curbside run --no-render                # qualify only, ~$0.001/address
```

Delete `var/` to start completely fresh — it holds the database and all
generated images.

## Adding your own addresses

Edit `data/addresses.txt`, one per line. They must be in a state with a
configured imagery source:

```bash
curbside sources
```

Indiana, Connecticut and North Carolina are wired. An address elsewhere
geocodes fine but returns no imagery.

## Troubleshooting

| Symptom | Cause |
|---|---|
| `GEMINI_API_KEY not set` | `source ~/.gemini_env` first |
| `return address not configured` | `source .env` — required to compose |
| `budget cap would be exceeded` | Raise `CURBSIDE_BUDGET` or use a fresh `--db` |
| `HTTP 429` from Gemini | Billing not enabled — image models have no free tier |
| Geocode misses | Address may not exist; OSM allows ~1 request/second |
| `outside coverage` | Address is outside the configured state's imagery |
| Dashboard shows nothing | API not running — `./run-local.sh api` |
