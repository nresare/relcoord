# relcoord version service

Small HTTP service for registering image versions and resolving the latest known version per image.

## Manifest diff comments

`POST /v1/diffcomment` reports what a config commit *would* do to the manifests
repositories, and comments the result on a pull request. It does the same work as
`POST /v1/change` — checks out the config commit, clones each configured output
repository, runs `manifest-builder`, and lets it create its manifest commit — but
stops there: nothing is pushed. The diff between the commit each manifests
checkout was cloned at and the commit `manifest-builder` created is what the
comment describes, so it is exactly the change a `/v1/change` for the same commit
would push.

```bash
curl -H 'content-type: application/json' \
  -d '{"config_repo": "https://github.com/example/system",
       "commit": "deadbeef", "pull_request": 42, "system": true}' \
  http://localhost:8080/v1/diffcomment
```

The request takes `config_repo`, `commit`, and the same optional `config_path`
and `system` fields as `/v1/change`; `image_repo` and `tag` are rejected, because
a diff reports what a config commit generates rather than registering a version.
`pull_request` is optional: with it, relcoord posts the comment to that pull
request of `config_repo`, which then has to be an `https://github.com` URL;
without it, the response carries the comment body it would have posted and
nothing is sent to GitHub.

The token that posts the comment is an idcat-issued installation token for
`config_repo`, taken from the same per-repository cache the clones use, so the
GitHub app configured under `[idcat]` needs permission to write pull request
comments on the repositories that call this endpoint.

A diff spanning every cluster a deployment generates for is more than a reviewer
wants to read, so `diff-output` names the single output to report on:

```toml
diff-output = "example-dev"
```

Only that output is generated, and only its manifests repository is cloned; the
other outputs are left alone. The name has to match one of the `[[output]]`
entries, which relcoord checks at startup. Without `diff-output`, a diff covers
every configured output, with a heading per manifests repository.

The `200` response body reports the diff and the comment:

```json
{
  "config_repo": "https://github.com/example/system",
  "commit": "deadbeef",
  "pull_request": 42,
  "generated": 37,
  "outputs": [
    {"name": "example-dev", "repository": "https://github.com/example/manifests",
     "directory": "example-dev", "generated": 37}
  ],
  "diffs": [
    {"repository": "https://github.com/example/manifests",
     "stat": " example-dev/api.yaml | 2 +-\n", "summary": "", "diff": "diff --git ..."}
  ],
  "comment": {"posted": true, "url": "https://github.com/...", "body": "..."}
}
```

Every generated manifest carries a deploy-id annotation, and a change to a shared
label or annotation rewrites every manifest that has it, so the comment
summarizes those repeated metadata-only changes and leaves them out of the diff it
shows. `diffs[].diff` is always the unabridged diff, which is where the comment
points a reader who needs the part it left out.

`Accept: text/event-stream` works here too, with the same event shapes as
`/v1/change` and phases `workspace`, `source-checkout`, `deploy-config`,
`manifests-checkout`, `generate`, `generated`, `diff`, `no-changes`, `comment`,
`commented` and `no-comment`.

## Progress streaming

`POST /v1/change` normally answers with a single `202` JSON body once the change
has been processed. A client that wants to display the steps as they happen can
send `Accept: text/event-stream` instead, and gets the same outcome delivered as
server-sent events:

```bash
curl -N -H 'accept: text/event-stream' -H 'content-type: application/json' \
  -d '{"config_repo": "https://github.com/example/config", "commit": "deadbeef"}' \
  http://localhost:8080/v1/change
```

The stream carries four kinds of event:

- `accepted` — sent immediately, with `config_repo`, `commit` and the `registered`
  image version, so the client can render something before any git work starts.
- `progress` — one per step, with a stable `phase` (`workspace`,
  `source-checkout`, `deploy-config`, `manifests-checkout`, `generate`,
  `generated`, `no-changes`, `commit`, `push`, `pushed`, `deployment-detection`),
  a human readable `message`, and a `detail` object with specifics of the step.
- `complete` — the same body the non-streaming response would have returned.
- `error` — a change that failed, carrying the `status`, `error` and `message`
  that the non-streaming response would have used.

Comment lines (`: keep-alive`) are sent while a step is slow, so intermediate
proxies do not treat the connection as dead.

Because the HTTP status is committed before any manifest work begins, a streamed
response is always `202` and processing failures arrive as a terminal `error`
event. Everything that can be rejected up front — validation, authentication,
the `system` role check, and image version registration — still fails with a
regular status code and JSON body, whatever the client accepts.

Disconnecting does not cancel a change in progress; the server finishes the work
and logs the outcome.

## Development

Run the test suite:

```bash
uv run pytest
```

Start the service locally:

```bash
uv run relcoord
```

By default the service uses in-memory storage. You can also select it explicitly:

```toml
[persistence]
backend = "in-memory"
```

See `relcoord.toml.example` for a remote SurrealDB backend configured with
idmouse-issued database tokens:

```toml
[persistence]
backend = "surrealdb"
uri = "ws://localhost:8000/"
namespace = "default"
database = "relcoord"

[persistence.idmouse]
url = "http://localhost:9000/token"
token-path = "/tmp/idmouse-bearer-token"
```

The service can also store image versions in DynamoDB:

```toml
[persistence]
backend = "dynamodb"
table-name = "relcoord-image-versions"
region-name = "eu-west-2"
```

The DynamoDB table must already exist with string partition key `pk` and string
sort key `sk`. AWS credentials are resolved using the standard boto3 provider
chain. For local development against DynamoDB Local, set `endpoint-url`.

Manifest generation outputs are configured with `[[output]]` entries. Each
output names a manifests repository, an optional directory inside that
repository, and optional variables passed to `manifest-builder`. When
`directory` is omitted, manifests are written at the repository root:

```toml
[[output]]
name = "example-dev"
repository = "https://github.com/example/manifests"
directory = "example-dev"

[output.vars]
cluster_name = "example-dev"
account_id = 111122223333
issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLEDEVCLUSTERID"
```
