# relcoord version service

Small HTTP service for registering image versions and resolving the latest known version per image.

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
