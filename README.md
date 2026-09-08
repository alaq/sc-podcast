# SoundCloud Podcast

Like DJ sets on SoundCloud and listen in Overcast, Apple Podcasts, or another RSS podcast app. The default feed is `kado-nyc/likes`, published as **ACSv3**.

RSS requests normally read a prepared snapshot from Upstash Redis. For a brand-new source, the first request discovers metadata and prepares up to **five playable episodes** before returning RSS. Background sync then fills the archive toward **200 episodes**. Existing subscribers keep their feed URL.

## Listening

- Feed: `https://sc-podcast.vercel.app/` (also `https://podcast.alaq.io/`).
- Subscription help and current sync status: `/about`.
- Subscribe directly to another SoundCloud source, for example `https://podcast.alaq.io/robot-heart/tracks`. No per-source configuration is required.
- In Apple Podcasts, use **Follow a Show by URL**. The help page also has an Overcast subscription link.
- New Likes are checked every five minutes. The CDN may retain the previous snapshot for another five minutes, and clients control their own polling/download schedules.
- Episodes use the Like date, stable IDs, fixed enclosure URLs, MP3 sizes, artwork, and durations. Existing episodes retain their seeded dates and enclosure identities during migration.
- Episode artwork uses SoundCloud's 3000×3000 rendition to meet [Apple Podcasts' artwork dimensions](https://podcasters.apple.com/support/5516-episode-art-template). Images load directly from SoundCloud; feed requests never fetch or resize them. Retained 500×500 artwork URLs are upgraded when the snapshot is next published.

Previously discovered sets remain in the archive; unliking does not retract a published episode. Only full progressive MP3s are included. Unavailable, preview-only, and HLS-only tracks are retried separately and do not block other episodes. A Like of an entire playlist is skipped; Like individual sets to include them. A configured playlist source supports its first 200 tracks.

## Add a SoundCloud page

Subscribe in your podcast app by replacing `soundcloud.com` with `podcast.alaq.io` in a public source URL. For example, Robot Heart uploads use `https://podcast.alaq.io/robot-heart/tracks` (or simply `/robot-heart`), and its Likes use `https://podcast.alaq.io/robot-heart/likes`. Profiles, `/likes`, `/reposts`, individual tracks and playlists are supported. Search, personalized home pages and private tracks are not.

The first RSS request registers the source, saves its first page of track metadata and pagination cursor, prepares five playable episodes, and returns usable RSS. It immediately queues background batches to grow the same feed toward 200 episodes. Concurrent first subscribers share preparation, including clients that probe with `HEAD` before `GET`. Adding the same source again reuses its feed across listeners.

Initial preparation targets an eight-second soft budget, with seven seconds for source work, short network timeouts and at most ten audio attempts to find five successes. It returns fewer episodes if the source has fewer playable tracks or the budget runs short; subsequent batches retain their IDs and dates. Exact source totals are not needed: saved metadata and pagination cursors let preparation resume. Only if no real episode can be prepared does the request return retryable `503` with `Retry-After: 5`; repeated cold attempts are coalesced for a minute. Network/storage latency can extend the soft budget. Once a snapshot exists, reads never wait for SoundCloud extraction.

For an `on.soundcloud.com` share link, open it on SoundCloud first and use the full source URL it resolves to.

Only the main ACSv3 feed uses the five-minute scheduler. Every other feed refreshes on demand: a request to its RSS URL (including HEAD or conditional 304 requests) or progress API can enqueue work if its last successful check is at least 30 minutes old. Incomplete preparation can resume sooner. No more requests means no more refreshes after the already-started, bounded preparation burst finishes; metadata, saved RSS and URLs remain available. An unsubscribe is not observable directly: an app or crawler that continues polling still counts as demand.

The first request after an idle period gets the saved feed while a one-off QStash job checks SoundCloud. A subsequent client poll sees any updates. The CDN can serve requests for five minutes before the next origin request; this delays demand detection slightly but does not generate periodic work itself. Enqueueing uses short connection/read timeouts, and a queue outage does not prevent a saved RSS response. A one-minute per-source cooldown prevents failed requests or rapid progress polling from repeatedly enqueueing work.

Each request-triggered burst can queue up to 25 short background batches, stopping once preparation is complete. Per-feed leases coalesce concurrent requests. A 500-message daily budget covers all secondary-feed refresh and preparation enqueues, leaving room for the main schedule; if it is exhausted, later client requests resume work after the UTC daily reset. Each retry still consumes QStash allowance. The default service limit is 50 automatically registered feeds and five new registrations per client IP per hour. Reopening an existing feed does not count toward that rate. These bounds prevent an anonymous endpoint from creating unlimited paid work.

## Architecture

```text
Main feed: QStash (every 5 min) → SoundCloud sync → atomic RSS publication
Other feeds: client request → saved RSS + one-off QStash work when due
New RSS URL → Redis registration + metadata → first five episodes → background batches
Podcast app → Vercel CDN → Redis manifest + saved RSS
Podcast app → /track/artist/set → temporary 302 → SoundCloud MP3 CDN
```

Redis holds compressed discovery state, fixed publication dates, permanent track-path mappings, current/previous RSS snapshots, and a two-minute cache of signed audio URLs. Audio files are never stored or proxied. The existing `KV_REST_API_*` variables work; the service is now Upstash Redis following Vercel KV's migration.

Feed responses support `HEAD`, gzip, ETags and `Last-Modified`. Unchanged conditional requests avoid reading the large RSS body; secondary feeds also read their small status record to decide whether to enqueue work. Snapshot publication is atomic and guarded by an expiring lock with an ownership check. Failed discovery/preparation leaves the previous feed available. A Redis outage can still affect an uncached origin read; the CDN is a cache, not an independent durable replica.

Sync saves discovered metadata before audio preparation and resumes after interruption. Background invocations process at most one extra listing page and eight audio items; the initial RSS request only reads the head page and stops at five successes or its time/attempt budget. Retryable tracks use backoff. Unchanged checks do not fetch the large saved state. The yt-dlp SoundCloud adapter is isolated in `podcast/soundcloud.py` and pinned because it uses extractor internals.

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
| `SC_FEEDS` | Legacy configured feeds with publication gates, default `kado-nyc/likes`; only the main feed is scheduled and new sources register automatically |
| `PUBLIC_BASE_URLS` | Origins to preserve in enclosures, default both existing domains |
| `SC_PODCAST_NAMESPACE` | Isolated key prefix, default `sc-podcast:v2`; existing timestamp keys are read without modification |
| `FEED_MAX_ITEMS` | Default 200 |
| `SYNC_SECRET` | Random bearer credential for manual sync requests |
| `QSTASH_CURRENT_SIGNING_KEY`, `QSTASH_NEXT_SIGNING_KEY` | Validate scheduled requests and key rotation |
| `QSTASH_TOKEN` | Schedule management and automatic background preparation |
| `QSTASH_URL` | Optional regional QStash API origin for enqueueing and the CLI, as shown in your console |
| `SYNC_URL` | Exact public `/api/sync` URL used as the QStash JWT audience |
| `MAX_AUDIO_PREPARATIONS`, `SYNC_BUDGET_SECONDS` | Default eight preparations and a 40-second soft work budget, within Vercel's 60-second function limit |
| `MAX_AUTO_FEEDS` | Maximum automatically registered sources, default 50 |
| `AUTO_REFRESH_SECONDS` | Minimum age for a normal request-triggered refresh, default 1800 seconds; incomplete preparation can resume sooner |

New sources do not need an allowlist edit, manual publication or any recurring schedule. Use `podcast.cli schedule create` for the main feed only. If upgrading from the shared-tick deployment, this updates the same existing schedule ID to `{"feed":"kado-nyc/likes"}`. The old `{"tick":true}` endpoint and `--shared` option remain compatible but now process only the main feed, ignoring historical secondary entries in the due index. `podcast.cli tick` also refreshes only the main feed. Snapshot and registration keys require no migration.

Preview deployments automatically append a branch hash to the Redis namespace and disable Overcast pings and background-message dispatch. A direct new RSS request still prepares its first episodes. To continue preparation in that namespace locally, use `VERCEL_ENV=preview` and the branch name in `VERCEL_GIT_COMMIT_REF`, then `podcast.cli --feed source/tracks sync`. For browser testing, include the preview origin in `PUBLIC_BASE_URLS`. Keep its `SYNC_URL` consistent with the deployment when testing signed requests, and account for deployment protection. Preview registration never queues production jobs.

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
   .venv/bin/python -m podcast.cli schedule create
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

The tests use synthetic SoundCloud responses and execute actual Redis commands/Lua through fakeredis. They cover cold GET/HEAD subscriptions, concurrent first subscribers, partial initial progress, immediate continuation, 200-episode batching, interrupted work, pagination gaps, unavailable-track recovery, locking, old-snapshot preservation, conditional responses, JWT authentication, and SQLite migration retries. Test runs do not need service credentials or send messages. Live source probes and hosted deployment checks remain separate.

`GET /status` exposes default-feed timestamps and counts. `POST /api/feeds` accepts a public SoundCloud `url`; `GET /api/feeds?source=user/tracks` returns preparation progress. `POST /api/sync` accepts `{"tick":true}` for main-feed work or `{"feed":"kado-nyc/likes"}` for one feed, with a QStash signature or the manual bearer credential. Keep tokens out of command history and request logs. `PING_OVERCAST` remains off; the main-feed tick does not send pings.

## Cost for ten listeners

Reuse the existing database and Vercel plan. One five-minute QStash schedule is **288 deliveries/day** (at most 864 with both retries every time), within the current 1,000/day free allowance before unrelated usage. Redis Free currently includes 256 MB, 500K commands/month, and 10 GB bandwidth. Redis PAYG is $0.20/100K commands; the ten-listener estimate of roughly 130K–245K monthly commands is about **$0.26–$0.49 in command charges** if paid. Storage is metadata/XML, and listeners download audio directly from SoundCloud.

These are planning estimates for the original single source, not an account bill or a promise for unlimited sources. The main recurring schedule still uses 288 initial messages/day. Secondary sources only consume refresh/preparation messages when requested: ten continuously polled, unchanged sources checked every 30 minutes would add about 480 initial messages/day; sources nobody requests add none. Initial 200-episode preparation can add about 25 messages per source. The shared 500-enqueue daily cap bounds secondary work across refreshes and preparation, with retries additional; a busy first day can defer some preparation until new requests arrive after the reset. Metadata commands and function work grow with distinct active sources. Client polling varies; gzip, CDN reuse and validators reduce Redis bandwidth. Check account-wide usage after rollout. No plan upgrade is required by this change. Prices verified 2026-09-05, QStash rechecked 2026-09-07: [Upstash Redis](https://upstash.com/pricing/redis), [QStash](https://upstash.com/pricing/qstash), [Vercel cron limits](https://vercel.com/docs/cron-jobs/usage-and-pricing).

## Rollback

Keep the recurring schedule addressed directly to the main feed when rolling back to the earlier shared-tick code; otherwise that code will resume periodic secondary refreshes. For a full scheduler rollback, pause it with `podcast.cli schedule pause` and restore the previous Vercel deployment. Keep the new Redis namespace and SQLite baseline: deleting either is unnecessary and could cause duplicate historical announcements. The new code does not change legacy timestamp keys. Resume only after the prepared snapshot and deployment agree. Monitor stale `last_success_at` and failed QStash deliveries rather than silently replacing a good RSS feed with an empty one.
