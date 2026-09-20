# API keys and the public repo

## The short answer: the published site uses no API keys at all

This is a **build-time** pipeline and a **static** site. Every API key is used
only while generating tiles on your machine. The thing GitHub Pages serves is:

```
web/index.html        web/src/app.js      web/src/app.css
web/config.json       web/states.geojson  web/tiles/*.pmtiles
```

At runtime the page fetches those files and nothing else. No EIA call, no
Census call, no Anthropic call, no proxy, no backend. Open the network tab on
the live site and you will see only same-origin requests.

So there is no key to leak, no quota to abuse, and nothing for a "BYO key"
field to do. **The absence of a backend is the security property**, not
something bolted on top of one.

## Where keys actually live

| Context | Storage | Exposed in a public repo? |
|---|---|---|
| Your machine | `.env`, gitignored, `chmod 600` | No — untracked |
| `refresh.yml` workflow | GitHub Actions **Secrets** | No — encrypted, masked in logs, and not passed to fork PRs |
| Published site | *not present* | N/A |

Verified before first publish:

- `.env` is untracked (`git ls-files` confirms).
- No key value appears in **any** object in git history (`git log -S` across all
  refs, all five keys, clean).
- No secret-shaped string appears in any published file.

Re-run that audit any time with `make audit`.

## If you ever add a runtime feature that needs a key

The moment the page itself calls a paid API, the calculus changes. The
industry-standard pattern, in order of preference:

1. **Keep it build-time.** Bake the result into the tiles. Always preferable.
2. **BYO key, client-held.** The viewer pastes their own key; store it in
   `localStorage` on their device only; never send it anywhere but the vendor's
   own endpoint. You are never liable for their quota and they are never
   exposed to yours.
3. **Your key behind a server proxy** with per-IP rate limiting, an allowlist
   of endpoints, and a hard monthly spend cap. Only do this if you accept
   paying for strangers' usage — a public proxy over your key *will* be abused.

Never ship your own key in client JavaScript, even "restricted". Anything the
browser can read, a scraper can read.

## Note on these particular keys

The five keys currently in `.env` were pasted into a chat session, so they
exist in that transcript. Nothing has leaked into the repo, but rotating them
is cheap and is good hygiene:

- EIA, Census, NREL/NLR: re-register, takes ~2 minutes each
- PeeringDB: Profile → API Keys → revoke and re-issue
- Anthropic: console → API Keys → revoke and create (this is the only one with
  real money attached)
