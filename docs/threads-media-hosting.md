# Threads image publishing: media hosting requirement

> **Resolved.** The storage moved to a host Meta can reach and image
> publishing now works. See
> [file-storage-migration.md](file-storage-migration.md). This document is
> kept for the diagnosis; the "options" below are historical, and option 3
> is what was effectively taken: moving to a different network rather than
> asking the original provider to fix its routing.

## Symptom

Container creation for `media_type=IMAGE` fails with:

```
HTTP 400
OAuthException, code 1, error_subcode 2207052, is_transient false
"Failed to download media file. Its URI does not meet our requirements."
```

Text posts, replies and threads are unaffected: they carry no `image_url`,
so Meta never has to download anything.

## Root cause

Meta downloads `image_url` from its own crawler network during container
creation. Those crawler prefixes (observed: `173.252.64.0/18`,
`69.63.176.0/20`) are **not routable from the storage VPS**
(the original storage host), while Meta's user-facing prefixes
(`157.240.0.0/16`, `31.13.0.0/16`) are.

The result is a half-open handshake:

1. the crawler sends `SYN` to the storage host on :443,
2. Caddy answers `SYN-ACK`,
3. the `SYN-ACK` never reaches the crawler, so no `ACK` and no TLS
   `ClientHello` ever arrive,
4. the crawler retries the `SYN` ~7 times and gives up,
5. Graph API reports subcode 2207052.

Nothing about the image, the Caddy configuration, the TLS certificate, the
DNS records or the request encoding is at fault: a byte-identical JPEG
publishes successfully when it is served from a host Meta can reach.

Because `is_transient` is `false` and the cause is a routing blackhole,
**retrying does not help**.

## What this means in practice

Single-image and carousel publishing work only when the media is served from
a host Meta's crawlers can reach. The storage server behind
`https://files.apps.leadmeter.ru` currently is not such a host.

Options, in order of effort:

1. Put a CDN that Meta can reach (for example Cloudflare) in front of the
   storage host and point `STORAGE_PUBLIC_BASE_URL` at it. No code change
   is required.
2. Mirror the media to object storage outside the affected network and point
   `STORAGE_PUBLIC_BASE_URL` there.
3. Ask the hosting provider to restore routing to Meta's crawler prefixes.

## Reproducing the diagnosis

```bash
uv run python scripts/diagnose_threads_media.py
```

The script creates (but does not publish) one container from an external
control URL and one from our storage, so an API/token/image problem can
never be confused with a storage-reachability problem.

### Do not use an outbound connect test

An earlier revision of this document suggested probing the crawler prefixes
with an outbound TCP connect from the storage host. **That test does not
work.** Those prefixes do not accept connections on :443 from anywhere, so
every host "fails" it, including hosts Meta can reach perfectly well. It was
measured on two VPSes: both timed out against `173.252.64.0/18` and
`69.63.176.0/20`, yet only one of them actually blocked the crawler.

The only reliable test is to create a real media container against a URL on
the host in question, with an external control URL alongside it to rule out
the token and the image. `scripts/diagnose_threads_media.py` does exactly
that and creates containers without publishing them.
