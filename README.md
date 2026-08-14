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

Every configured output is generated, because which of them a commit affects is
not something the commit says: it is what generating shows. The comment then
covers the manifests repositories that changed, and leaves the ones the commit
generates nothing for out of it, so a change to a section only one cluster is
built from reads as that cluster's diff rather than as a wall of unchanged
clusters. A comment left with a single repository renders without a heading, and
one covering several heads each repository's diff with its URL; a change no
output is affected by comments that the generated output is unchanged.

`outputs` and `diffs` in the response still report every output that was
generated and every repository that was diffed, unaffected ones included, which
is where a reader who wants to see that a cluster was considered and left alone
finds it.

The `200` response body reports the diff and the comment:

```json
{
  "message": "1 manifests repository would change, commented",
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
`/v1/change` and phases `source-checkout`, `deploy-config`, `plugins-checkout`,
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

- `accepted` — sent immediately, with a `message`, `config_repo`, `commit` and the
  `registered` image version, so the client can render something before any git
  work starts.
- `progress` — one per step, with a stable `phase` (`source-checkout`,
  `deploy-config`, `plugins-checkout`, `rollout-stage`, `manifests-checkout`,
  `generate`, `generated`, `changed-objects`, `no-changes`, `push`, `pushed`,
  `deployment-detection`, `rollout-stage-verified`),
  a human readable `message`, and a `detail` object with specifics of the step.
- `complete` — the same body the non-streaming response would have returned,
  whose `message` says where the change was deployed.
- `error` — a change that failed, carrying the `status`, `error` and `message`
  that the non-streaming response would have used.

Every event carries a `message` written to be read on its own, so a client can
print the stream as it arrives without knowing any payload shape. What a message
leaves out is in `detail`: the temporary workspace, the full commit hashes, the
deploy-id, every changed object where the message named the first few. A step
whose only interest is to whoever debugs relcoord — the temporary directory it
works in, the manifests commit named by the push lines either side of it — is
logged rather than streamed, which is why there is no `workspace` or `commit`
phase.

Comment lines (`: keep-alive`) are sent while a step is slow, so intermediate
proxies do not treat the connection as dead.

Because the HTTP status is committed before any manifest work begins, a streamed
response is always `202` and processing failures arrive as a terminal `error`
event. Everything that can be rejected up front — validation, authentication,
the `system` role check, and image version registration — still fails with a
regular status code and JSON body, whatever the client accepts.

Disconnecting does not cancel a change in progress; the server finishes the work
and logs the outcome.

## Versions

`--version` reports the running release and the manifest-builder it generates
manifests with, and the same line is logged at startup:

```bash
relcoord --version
```

```
relcoord 0.1.0-38225f79 (manifest-builder 0.7.4)
```

In a container image that is the tag the image was published under: the nearest
reachable `vX.Y.Z` git tag and a hash of the build context, which together name
one build rather than only the release it came from. The publish workflow passes
it to the build, which writes it to `/usr/share/relcoord/image-tag` — the final
image has no shell to write it with, and nothing mounts over `/usr/share`, unlike
`/etc/relcoord`, where a mounted CA certificate would hide it.

Outside a container image there is no tag, and the version is the one the package
was built with. That comes from the same nearest reachable git tag, via
`hatch-vcs`, so a checkout reports something like `0.1.1.dev103+g3269f1e`. The
container build has no `.git` to read — copying it in would rebuild the layers
above it on every commit — so the workflow passes the version in as well.

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

`manifest-builder` config directories come in two layouts, told apart by the
`version` field of the top-level `config.toml` (or `manifest-builder.toml`) of
the commit being processed. A directory that declares config blocks directly
(`version = 1`, or no `version` at all) is rendered with the `vars` of the
output. A directory that declares `[[target]]` entries (`version = 2`) instead
picks one target by name, and relcoord passes the output's `target`:

```toml
[[output]]
name = "example-dev"
repository = "https://github.com/example/manifests"
directory = "example-dev"
target = "dev"
```

`target` defaults to the name of the output, so it only needs setting when the
targets in the config repositories are named differently. Which of the two
applies is a property of each config commit rather than of the output, so an
output serving both kinds of repository keeps its `vars` and its `target`
configured side by side.

## Deployment detection

With `detect-deployment = true`, a change does not end at the manifests push:
relcoord goes on to watch the cluster the manifests are deployed to until the
change has actually materialised there, and logs it when it has.

What it waits for comes from `manifest-builder`, which reports the Kubernetes
objects each commit touched — kind, namespace where the object has one, and
name — and stamps every manifest it wrote with a `noa.re/deploy-id` annotation
identifying that generation. relcoord reports both in the `changed-objects`
progress event and in the `outputs` of the change response, and then waits for
each created or modified object to carry that deploy-id and each removed object
to be gone. Objects are waited for with a list narrowed to the object's name
followed by a watch, so a rollout is observed as it happens rather than polled
for.

Carrying the deploy-id says the write landed, not that it took effect, so for the
kinds that roll a write out to pods the wait goes further. A Deployment has to
have had its new generation observed, its new ReplicaSet scaled up, the old ones
drained and the new pods become available; a StatefulSet has to have had its new
generation observed, every replica become ready and the replicas its update
strategy covers updated to the new revision. These are the checks `kubectl
rollout status` makes, and like it relcoord treats a partitioned StatefulSet
rollout as complete once the ordinals above the partition are updated, since the
ordinals below it are held back on purpose. A StatefulSet updated `OnDelete` has
nothing rolling the change out — its pods are replaced whenever someone deletes
them — so there the write landing is as far as detection waits. A Deployment the
cluster has given up on, one that is paused or one whose rollout exceeded its
progress deadline, fails the wait immediately rather than waiting out the
timeout.

The connection properties live on the output whose deployment they describe.
With deployment detection enabled, every output must explicitly set
`connection-type` to either `eks` or `local`:

```toml
detect-deployment = true

[[output]]
name = "example-dev"
repository = "https://github.com/example/manifests"
directory = "example-dev"
connection-type = "eks"
api-endpoint = "https://EXAMPLEDEVCLUSTERID.gr7.eu-west-1.eks.amazonaws.com"
ca-path = "/etc/relcoord/example-dev-ca.pem"
region = "eu-west-1"
```

EKS authentication uses a presigned STS `GetCallerIdentity` URL signed as
relcoord's own identity and re-signed as it ages. The cluster therefore needs
an access entry for relcoord's IAM role, granting at least `get`, `list` and
`watch` on the objects it deploys — and because the token is signed rather than
fetched, that role can live in a different AWS account from the cluster.

The API endpoint and CA certificate are configured rather than looked up:
`eks:DescribeCluster` only resolves clusters in the caller's own account, so
there is no call that returns them for a cluster elsewhere. Both come from, run
in the cluster's own account:

```bash
aws eks describe-cluster --name example-dev --query 'cluster.{endpoint:endpoint,ca:certificateAuthority.data}'
```

`ca-path` points at that CA decoded from base64 into a PEM file. `region`
defaults to the region of relcoord's AWS session, and `eks-cluster-name` to the
name of the output.

To watch the cluster relcoord itself runs in, configure a local connection:

```toml
[[output]]
name = "local"
repository = "https://github.com/example/manifests"
directory = "local"
connection-type = "local"
```

Local connections use `https://kubernetes.default.svc` and the service-account
CA certificate and bearer token mounted under
`/var/run/secrets/kubernetes.io/serviceaccount`. `api-endpoint` and `ca-path`
can still be set explicitly when the in-cluster defaults are not suitable. The
service account needs `get`, `list`, and `watch` access to deployed objects.

The `relcoord-eks-kubeconfig` command writes the same credentials into a
kubeconfig context, which is the quickest way to check that a cluster's access
entry works before turning detection on:

```bash
relcoord-eks-kubeconfig example-dev --region eu-west-1 \
  --endpoint https://EXAMPLEDEVCLUSTERID.gr7.eu-west-1.eks.amazonaws.com \
  --ca-file /etc/relcoord/example-dev-ca.pem
```

## Rollouts

Without a rollout, a change deploys every output at once: the manifests are
pushed, and relcoord follows each deployment into its cluster in the background.
A `[[rollout]]` puts an order on that instead. Its stages are deployed one at a
time, and a stage is not started until every deployment of the stage before it
has been observed in its cluster:

```toml
detect-deployment = true

[[rollout]]
name = "linear"

[[rollout.stage]]
outputs = ["platform-dev"]

[[rollout.stage]]
outputs = ["platform-prod", "observability"]
```

Which outputs a change affects is not configured. An output whose manifests the
config commit leaves alone generates no changes, so it is not pushed and there is
no deployment of it to wait for — it drops out of the rollout for that change.
That is what makes one pipeline serve config repositories whose targets are built
from different sections: a change to a section every target shares walks the
whole pipeline, dev first, while a change to a section only `observability` is
built from finds the first stage empty and reaches `observability` straight away.

Because a stage waits, `[[rollout]]` requires `detect-deployment = true`. Every
output must appear in exactly one stage of one rollout: an output named twice
would have two stages pushing to one cluster, and an output named nowhere would
deploy outside the rollout that was configured to order it. Rollouts are walked
in the order they are configured, one at a time, so several rollouts express
pipelines that are independent rather than pipelines that run concurrently.

A deployment a stage waits for and does not observe within the detection timeout
fails the change with `rollout_stage_failed`, and the stages after it are neither
generated nor pushed. What earlier stages pushed stays pushed, which is the point
of deploying to `platform-dev` first.

A rollout therefore holds a `/v1/change` request open for as long as the whole
pipeline takes, waiting included. `Accept: text/event-stream` is the way to watch
it happen: `rollout-stage` reports the stage being deployed and
`rollout-stage-verified` the outputs whose deployment it observed, and the
`outputs` of the response say which rollout and stage each output was deployed
by.

Outputs sharing a manifests repository are pushed as one commit per stage rather
than one commit per change, since a stage cannot push what a later stage has not
generated yet.
