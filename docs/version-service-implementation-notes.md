# Image Version Service Implementation Notes

## Purpose

This document records implementation-facing decisions that should stay out of the service interface specification.

The intent is to keep [version-service-design.md](/Users/noa/fun/relcoord/docs/version-service-design.md) focused on externally visible behavior while this document captures the technology and engineering choices used to build the service.

## Current Decisions

| Topic | Decision | Notes |
| --- | --- | --- |
| Programming language | Python | Requested implementation language |
| Module name | `relcoord` | Use `relcoord` as the Python package/module name |
| Web framework | Starlette | ASGI application framework |
| Server | Embedded Hypercorn | Run Hypercorn in-process rather than as an external runtime dependency |
| Persistence | SurrealDB | Existing SurrealDB instance in the Kubernetes cluster |
| Dependency management | uv | Use `uv` for local development, dependency management, and reproducible environments |
| Packaging | Python wheel | Build and distribute the service as a wheel |
| Deployment model | Kubernetes | Expected to run in-cluster |
| Authentication | None for MVP | Treat the service as internal-only for now |
| Logging and metrics | TBD | |
| Testing strategy | pytest | Use `pytest` as the primary test runner and test framework |

## Constraints And Implications

### Python

- The codebase should be structured as a small ASGI application.
- The Python package name should be `relcoord`.
- Dependency management should support straightforward local development and containerized deployment.
- Project metadata and build configuration should be compatible with wheel packaging.
- Local workflows should be documented in terms of `uv`.

### Starlette

- Route handling, request validation boundaries, and middleware should align with Starlette conventions.
- If schema generation is needed later, it may require an additional library because Starlette itself is intentionally minimal.

### Hypercorn

- The service should expose a standard ASGI app object suitable for Hypercorn.
- The service should start Hypercorn programmatically from Python rather than relying on a separate `hypercorn` process invocation.
- Deployment documentation should describe the embedded-server startup path and its configuration surface.

### SurrealDB

- Persistence logic should be designed around a remote database rather than local embedded storage.
- Startup and runtime behavior should assume database connectivity to an existing in-cluster SurrealDB instance.
- The implementation should define how uniqueness and idempotency are enforced in SurrealDB for `(image, version)`.
- Each `(image, version)` record should store a timestamp.
- When registration omits a timestamp, the application should assign the current call time before persisting the record.

## Choices Still To Make

### Timestamp handling

Questions:

- Should the application or SurrealDB be the source of truth for the default registration timestamp?
- Which Python type and parser should be used for RFC 3339 timestamp validation?
- How should the service handle clock skew between callers that supply timestamps and callers that omit them?

Decision:

- TBD

Notes:

- 

### Data modeling in SurrealDB

Questions:

- Should versions be stored as one record per `(image, version)` pair?
- Should image names be stored as raw fields, normalized identifiers, or both?
- How should timestamp conflicts be detected and rejected when different versions for the same image use the same timestamp?
- Which index should support resolving the latest version for an image efficiently?

Decision:

- TBD

Notes:

- 

### Request validation

Questions:

- Should request and response models use dataclasses, Pydantic, or manual validation?
- Do we want strict schema enforcement at the HTTP boundary?

Decision:

- TBD

Notes:

- 

### Authentication and authorization

Questions:

- None for MVP.
- Revisit authentication only if the service boundary expands beyond trusted internal callers.

Decision:

- No authentication or authorization in the MVP.

Notes:

- This keeps the interface and deployment simpler while the service remains internal-only.

### Deployment packaging

Questions:

- How should the wheel be installed into the runtime image?
- What base image should we use around the embedded Hypercorn process?
- How will configuration such as SurrealDB connection details be supplied?

Decision:

- Package the service as a Python wheel and run it in Kubernetes.

Notes:

- `uv` should be the default tool for dependency installation, lockfile management, and local execution.

### Observability

Questions:

- What logging format should we use?
- Which metrics should be exported?
- Do we need structured request logging from the start?

Decision:

- TBD

Notes:

- 

### Testing

Questions:

- Which levels of testing are required for the MVP?
- Do we want integration tests against a real or containerized SurrealDB instance?
- Should timestamp parsing, defaulting, and conflict behavior be covered with parametrized `pytest` tests?

Decision:

- Use `pytest`.

Notes:

- Prefer a mix of unit tests for timestamp handling and request validation behavior, plus targeted integration tests around persistence and API flows.

## Suggested Next Decisions

If we want to unblock implementation quickly, the highest-value remaining choices are:

1. Timestamp validation and defaulting approach
2. Request validation approach
3. SurrealDB record layout and uniqueness strategy
4. Observability defaults
