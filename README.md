# relcoord version service

Small HTTP service for registering image versions and resolving the latest known version per image.

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
