# SoundCloud Podcast

Like DJ sets on SoundCloud and listen in Overcast, Apple Podcasts, or another RSS podcast app. The default feed is `kado-nyc/likes`, published as **ACSv3**.

RSS requests read a prepared snapshot from Upstash Redis. They never call SoundCloud or extract tracks. A separate authenticated sync discovers Likes, prepares playable MP3 metadata, and atomically publishes up to **200 episodes**. Existing subscribers keep their feed URL.

## Listening

- Feed: `https://sc-podcast.vercel.app/` (also `https://podcast.alaq.io/`).
- Subscription help and current sync status: `/about`.
- Add another SoundCloud source: `/add`. No per-source configuration is required.
- In Apple Podcasts, use **Follow a Show by URL**. The help page also has an Overcast subscription link.
- New Likes are checked every five minutes. The CDN may retain the previous snapshot for another five minutes, and clients control their own polling/download schedules.
- Episodes use the Like date, stable IDs, fixed enclosure URLs, MP3 sizes, artwork, and durations. Existing episodes retain their seeded dates and enclosure identities during migration.
- Episode artwork uses SoundCloud's 3000×3000 rendition to meet [Apple Podcasts' artwork dimensions](https://podcasters.apple.com/support/5516-episode-art-template). Images load directly from SoundCloud; feed requests never fetch or resize them. Retained 500×500 artwork URLs are upgraded when the snapshot is next published.

Previously discovered sets remain in the archive; unliking does not retract a published episode. Only full progressive MP3s are included. Unavailable, preview-only, and HLS-only tracks are retried separately and do not block other episodes. A Like of an entire playlist is skipped; Like individual sets to include them. A configured playlist source supports its first 200 tracks.

## Add a SoundCloud page

1. Open `https://sc-podcast.vercel.app/add` and paste a public SoundCloud URL. Profiles use uploads; `/likes`, `/reposts`, individual tracks, playlists and `on.soundcloud.com` share links are supported. Search, personalized home pages and private tracks are not.
2. The page creates a shared, stable feed URL and displays preparation progress. Subscription controls appear once the first real playable episodes are published.
3. Subscribe immediately; the archive grows in background batches toward 200 episodes. Adding the same source again reuses its feed, even across listeners.

Requesting a new RSS path such as `/robot-heart/tracks` also starts registration automatically. While its first episodes are pending, it returns a fast `503` with `Retry-After: 5` and a preparation-page link. Some podcast apps reject a brand-new empty/pending feed, so use `/add` for reliable first-time subscription. Once ready, feed reads only retrieve the saved snapshot.

One shared five-minute scheduler services registered feeds. Configured `SC_FEEDS` retain their five-minute cadence and existing migration gate; new sources normally refresh every 30 minutes, with extra work during initial preparation. Automatically added feeds with no origin requests for 30 days pause polling and resume when requested again. Metadata and URLs are retained.

The first addition can queue up to 25 short background batches. Per-feed leases coalesce concurrent requests, and a 500-message daily bootstrap budget leaves room for the shared schedule; remaining preparation continues through scheduled ticks. Each retry still consumes QStash allowance. The default service limit is 50 automatically registered feeds and five new registrations per client IP per hour. Reopening an existing feed does not count toward that rate. These bounds prevent an anonymous endpoint from creating unlimited paid work.

## Architecture

```text
Add URL → Redis registration → bounded QStash preparation batches
QStash (one shared 5 min tick) → due feeds → SoundCloud discovery + bounded audio preparation
                                     ↓ atomic publication
Podcast app → Vercel CDN → Redis manifest + saved RSS
Podcast app → /track/artist/set → temporary 302 → SoundCloud MP3 CDN
```

Redis holds compressed discovery state, fixed publication dates, permanent track-path mappings, current/previous RSS snapshots, and a two-minute cache of signed audio URLs. Audio files are never stored or proxied. The existing `KV_REST_API_*` variables work; the service is now Upstash Redis following Vercel KV's migration.

Feed responses support `HEAD`, gzip, ETags and `Last-Modified`. Unchanged conditional requests only read the small manifest. Snapshot publication is atomic and guarded by an expiring lock with an ownership check. Failed discovery/preparation leaves the previous feed available. A Redis outage can still affect an uncached origin read; the CDN is a cache, not an independent durable replica.

Sync saves discovered metadata before audio preparation, processes at most one extra listing page and eight audio items per invocation, and resumes after interruption. Retryable tracks use backoff. Unchanged checks do not fetch the large saved state. The yt-dlp SoundCloud adapter is isolated in `podcast/soundcloud.py` and pinned because it uses extractor internals.

## Setup

Requires Python 3.12 and an existing Upstash Redis database.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements-dev.txt
cp .env.example .env
```

Fill `.env` locally; it is gitignored. Configure the same variables in Vercel:

| Variable | Purpose |
| --- | --- |
| `KV_REST_API_URL`, `KV_REST_API_TOKEN` | Existing Upstash database REST connection; `UPSTASH_REDIS_REST_*` aliases also work |
| `SC_FEEDS` | Existing configured feeds to keep on five-minute refreshes, default `kado-nyc/likes`; new sources register automatically |
| `PUBLIC_BASE_URLS` | Origins to preserve in enclosures, default both existing domains |
| `SC_PODCAST_NAMESPACE` | Isolated key prefix, default `sc-podcast:v2`; existing timestamp keys are read without modification |
| `FEED_MAX_ITEMS` | Default 200 |
| `SYNC_SECRET` | Random bearer credential for manual sync requests |
| `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | Validate scheduled requests and key rotation |
| `QSTASH_TOKEN` | Schedule management and automatic background preparation |
| `QSTASH_URL` | Optional regional QStash API origin for the CLI, as shown in your console |
| `SYNC_URL` | Exact public `/api/sync` URL used as the QStash JWT audience |
| `MAX_AUDIO_PREPARATIONS`, `SYNC_BUDGET_SECONDS` | Default eight preparations and a 40-second soft work budget, within Vercel's 60-second function limit |
| `MAX_AUTO_FEEDS` | Maximum automatically registered sources, default 50 |
| `AUTO_REFRESH_SECONDS` | Normal refresh interval for new sources, default 1800 seconds |

New sources do not need an allowlist edit, manual publication or their own schedule. At deployment, update the existing recurring job once with `podcast.cli schedule create --shared`. This reuses the existing schedule ID, changes its body to `{"tick":true}`, and keeps the existing feed's publication gate intact. `podcast.cli tick` runs one tick manually. On rollback to code without the registry, restore a per-feed schedule with `podcast.cli schedule create`.

Preview deployments automatically append a branch hash to the Redis namespace and disable Overcast pings and background-message dispatch. To prepare that same namespace locally, use `VERCEL_ENV=preview` and the branch name in `VERCEL_GIT_COMMIT_REF`, then `podcast.cli --feed source/tracks sync` after registering through the preview page. For browser testing, include the preview origin in `PUBLIC_BASE_URLS`. Keep its `SYNC_URL` consistent with the deployment when testing signed requests, and account for deployment protection. Preview registration never queues production jobs.

## Safe rollout of the existing feed

Do this against the new production namespace **while the old deployment is still serving**. Do not enable the scheduler or promote the new code before preparing its snapshot.

1. Verify the Vercel project, existing Redis identity/plan, QStash plan, and both public feed origins. Retain the old deployment for rollback. Set the environment variables without changing billing plans.
2. Capture existing enclosure identities and dates, then prepare the archive in resumable batches:

   ```sh
   .venv/bin/python -m podcast.cli seed
   .venv/bin/python -m podcast.cli sync --runs 40
   .venv/bin/python -m podcast.cli status
   .venv/bin/python -m podcast.cli export
   ```

   `migration-data/candidate-0.xml` and `candidate-1.xml` are reviewable RSS files. `historical-announcements.json` includes all discovered historical sets, including unavailable ones that might recover later. Confirm 200 ready episodes and stable legacy enclosures/dates. An incomplete run is resumable; publication requires 200 unless an explicit `--minimum-items` override is provided.
3. Capture the old feed again immediately before cutover, so Likes that reached the old feed during preparation retain its identity. Then baseline the existing WhatsApp consumer and publish the saved snapshot:

   ```sh
   .venv/bin/python -m podcast.cli seed
   .venv/bin/python -m podcast.cli publish \
     --announcement-db "$HOME/.local/share/podcast-channel-automation/state.sqlite"
   ```

   This acquires the **same `run.lock` as the announcement worker**, creates a SQLite backup with mode `0600`, inserts only unseen historical identities as `baseline`, preserves all pending/announced rows, and then commits the Redis snapshot before releasing the worker lock. It never invokes the worker or sends a message. New Likes after bootstrap and items from the old live feed remain eligible for normal announcements. If Redis publication fails after baselining, rerunning is safe.
4. Deploy/promote the new code. Verify both feed origins before enabling QStash. A public `GET` before bootstrap returns retryable 503, so preparation must precede production promotion.
5. Create/update the schedule and read it back:

   ```sh
   .venv/bin/python -m podcast.cli schedule create --shared
   .venv/bin/python -m podcast.cli schedule status
   ```

   The stable schedule ID makes retries update the existing schedule. Delivery uses a signed POST every five minutes, a 60-second timeout, and at most two retries. The endpoint checks the body hash, exact destination, issuer, expiry, and current/next signing keys. No sync token is forwarded by QStash.
6. Read back RSS counts, HEAD, conditional 304, MP3 redirect/range behavior, `/status`, QStash delivery logs, and the announcement database counts. On devices, confirm existing play state and downloads in Overcast and Apple Podcasts, including seeking/resuming an older set. Local protocol tests cannot prove a particular app version's migration behavior.

Automatically registered sources publish incrementally as soon as real episodes are ready and have no announcement consumer attached. For a manually prepared legacy/isolated feed **with no announcement consumer**, `publish --no-announcement-consumer` remains available. Never bypass the ACSv3 production migration gate.

## Development and verification

```sh
.venv/bin/python -m pytest -q
.venv/bin/python local_server.py
```

The tests use synthetic SoundCloud responses and execute actual Redis commands/Lua through fakeredis. They cover 200-episode batching, interrupted work, pagination gaps, unavailable-track recovery, locking, old-snapshot preservation, conditional responses, concurrent readers, JWT authentication, and SQLite migration retries. Test runs do not need service credentials or send messages. Live source probes and hosted deployment checks remain separate.

`GET /status` exposes default-feed timestamps and counts. `POST /api/feeds` accepts a public SoundCloud `url`; `GET /api/feeds?source=user/tracks` returns preparation progress. `POST /api/sync` accepts `{"tick":true}` for shared work or `{"feed":"kado-nyc/likes"}` for one feed, with a QStash signature or the manual bearer credential. Keep tokens out of command history and request logs. `PING_OVERCAST` remains off; the shared tick does not send pings.

## Cost for ten listeners

Reuse the existing database and Vercel plan. One five-minute QStash schedule is **288 deliveries/day** (at most 864 with both retries every time), within the current 1,000/day free allowance before unrelated usage. Redis Free currently includes 256 MB, 500K commands/month, and 10 GB bandwidth. Redis PAYG is $0.20/100K commands; the ten-listener estimate of roughly 130K–245K monthly commands is about **$0.26–$0.49 in command charges** if paid. Storage is metadata/XML, and listeners download audio directly from SoundCloud.

These are planning estimates for the original single source, not an account bill or a promise for unlimited sources. The shared recurring schedule still uses 288 initial messages/day regardless of source count, plus bounded preparation messages and retries. Ten new 200-episode sources can require approximately 250 preparation messages if all batches fill; metadata commands and function work also grow with distinct sources. Client polling varies; gzip, CDN reuse and validators reduce Redis bandwidth. Check account-wide usage after rollout. No plan upgrade is required by this change. Prices verified 2026-09-05, QStash rechecked 2026-09-07: [Upstash Redis](https://upstash.com/pricing/redis), [QStash](https://upstash.com/pricing/qstash), [Vercel cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).

## Rollback

Pause this schedule with `podcast.cli schedule pause` and restore the previous Vercel deployment. Keep the new Redis namespace and SQLite baseline: deleting either is unnecessary and could cause duplicate historical announcements. The new code does not change legacy timestamp keys. Resume only after the prepared snapshot and deployment agree. Monitor stale `last_success_at` and failed QStash deliveries rather than silently replacing a good RSS feed with an empty one.
