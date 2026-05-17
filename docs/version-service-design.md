# Image Version Service Interface

## Status

Draft MVP interface definition.

## Summary

This document defines the external interface of a small HTTP service that keeps track of known versions for container images and returns the latest known version for each image according to a timestamp stored with each image/version pair.

The purpose of this service is to remove the need to create configuration-only pull requests when deploying a newly built software version. Instead of checking image tag updates into the same git-managed configuration repository used by `manifest-builder`, `manifest-builder` will query this service at manifest generation time.

For the first iteration, the service has no concept of environments, promotion, deployment history, or test results. It only stores known versions for images, records when each version became known, and answers "what is the latest known version?".

## Goals

- Allow external systems to register that a specific version exists for a specific image.
- Return the latest known version for a given image using the stored timestamp for each image/version pair.
- Return latest versions for multiple images in one request so `manifest-builder` can resolve all required tags efficiently.
- Keep the interface small and stable.
- Preserve a clean path toward future policy-based selection, such as promotion based on deployment and test outcomes.

## Non-goals

- Environment-specific version selection.
- Automated promotion or rollout policy.
- Tracking deployments, test executions, or release eligibility.
- Image discovery from container registries in the MVP.

## Context

`manifest-builder` already generates Kubernetes manifests from configuration files stored in git. This is convenient for configuration changes, but it creates friction for repetitive image tag bumps. Those updates do not meaningfully change the desired deployment shape; they only identify which built artifact should be used.

This interface splits those responsibilities:

- `manifest-builder` remains responsible for deployment structure and configuration.
- The new service becomes responsible for image version knowledge.

## Service Responsibilities

The service has two required responsibilities:

1. Accept that a specific version of a specific image exists.
2. Store a timestamp for each image/version pair and return the version with the latest timestamp for one or more images.

## API

All endpoints are versioned under `/v1`.

Image names may contain `/`, `:`, `.`, and other characters that are awkward in path parameters, so image identifiers are passed in JSON bodies.

### Register a known image version

`POST /v1/image-versions`

Request:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2",
  "timestamp": "2026-05-17T10:15:30Z"
}
```

`timestamp` is optional. If it is omitted, the service records the time at which it handles the registration request.

Response when the version was newly recorded:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2",
  "timestamp": "2026-05-17T10:15:30Z",
  "created": true
}
```

Response when the version was already known:

```json
{
  "image": "registry.example.com/payments/api",
  "version": "1.4.2",
  "timestamp": "2026-05-17T10:15:30Z",
  "created": false
}
```

Behavior:

- The operation is idempotent.
- Re-registering the same `(image, version)` pair is successful and returns `created: false`.
- Re-registering an existing `(image, version)` pair does not change its stored timestamp.
- The service must reject invalid requests as described in the validation section.

### Get the latest known versions for images

`POST /v1/images/latest`

Request:

```json
{
  "images": [
    "registry.example.com/payments/api",
    "registry.example.com/payments/worker"
  ]
}
```

Response:

```json
{
  "versions": {
    "registry.example.com/payments/api": "1.4.2",
    "registry.example.com/payments/worker": "2.1.0"
  }
}
```

If an image has no known versions, the response remains successful and includes `null` for that image:

```json
{
  "versions": {
    "registry.example.com/payments/api": null
  }
}
```

### Health endpoint

`GET /healthz`

Response:

```json
{
  "status": "ok"
}
```

## Validation Rules

### Image field

- Required.
- Must be a non-empty string.
- Treated as an opaque identifier.

### Version field

- Required.
- Must be a non-empty string.
- Treated as an opaque version or tag identifier.

### Timestamp field

- Optional for `POST /v1/image-versions`.
- If supplied, must be a valid RFC 3339 timestamp with an explicit timezone offset.
- If omitted, the service must use the current time of the registration call.
- Stored timestamps should be normalized to UTC in responses.

### Images list

- Required for `POST /v1/images/latest`.
- Must be a JSON array of non-empty strings.
- Duplicate image names may be accepted, but the response must contain each image at most once.

## Timestamp Selection Rules

The latest version for an image is the version whose image/version record has the greatest stored timestamp.

If multiple versions for the same image have the same timestamp, the service must return a deterministic result. The MVP should avoid relying on tie-breaking behavior by treating equal timestamps for different versions of the same image as an invalid registration conflict.

## Response Semantics

### Registration responses

- If the image/version pair was not previously known, the service returns success with `created: true`.
- If the image/version pair was already known, the service returns success with `created: false`.
- Registration responses include the timestamp stored for the image/version pair.

### Latest-version responses

- If an image has one or more known versions, the response contains the version string with the latest stored timestamp for that image.
- If an image has no known versions, the response contains `null` for that image.

## Error Handling

Recommended response behavior:

- `200 OK` for successful reads.
- `200 OK` for idempotent duplicate registration with `created: false`.
- `201 Created` for first-time registration if the implementation wants to distinguish creation.
- `400 Bad Request` for malformed JSON, missing fields, invalid timestamps, or timestamp conflicts.
- `500 Internal Server Error` for unexpected failures.

Example invalid timestamp response:

```json
{
  "error": "invalid_timestamp",
  "message": "timestamp must be a valid RFC 3339 timestamp with timezone"
}
```

## Integration Contract with `manifest-builder`

`manifest-builder` remains the component that knows which images are relevant to a manifest. The version service does not need to understand Kubernetes resources or application topology.

Recommended interaction:

1. `manifest-builder` determines which images require version resolution.
2. It calls `POST /v1/images/latest` with that image list.
3. It receives a map from image name to version or `null`.
4. It injects those versions into the manifest generation process.
5. If any required image resolves to `null`, `manifest-builder` decides whether to fail or apply fallback behavior.

## Future Evolution

This interface intentionally leaves room for a smarter release selection system.

Future additions could include:

- environment-aware desired versions
- deployment history per environment
- automated test result ingestion
- promotion rules such as "eligible for staging after successful deployment in dev"
- policy-based selection that returns the best eligible version rather than simply the latest known version

The MVP should keep this interface stable unless those future capabilities require additional endpoints.

## Open Questions

- Should `manifest-builder` fail hard when an image has no known version, or should it allow local fallback rules?
- Which component is responsible for calling the registration endpoint: CI, a release job, or some registry-watching process?
- Should latest-version responses include the selected timestamp as well as the version string?
