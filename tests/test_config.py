# SPDX-License-Identifier: MIT
# SPDX-FileCopyrightText: 2026 PortSwigger Ltd
from pathlib import Path

import pytest

from relcoord.auth import KUBERNETES_CA_CERT_PATH, KUBERNETES_SERVICE_HOST
from relcoord.config import Settings


def test_settings_parse_surrealdb_idmouse_config(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idmouse-token"
    token_file.write_text("local-bearer-token\n")
    config.write_text(
        f"""
        host = "127.0.0.1"
        port = 9000

        [persistence]
        backend = "surrealdb"
        uri = "ws://surrealdb:8000/"
        namespace = "default"
        database = "relcoord"

        [persistence.idmouse]
        url = "http://idmouse:9000/token"
        token-path = "{token_file}"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.listen == "127.0.0.1"
    assert settings.port == 9000
    assert settings.persistence is not None
    assert settings.persistence.backend == "surrealdb"
    assert settings.persistence.uri == "ws://surrealdb:8000/"
    assert settings.persistence.namespace == "default"
    assert settings.persistence.database == "relcoord"
    assert settings.persistence.idmouse is not None
    assert settings.persistence.idmouse.url == "http://idmouse:9000/token"
    assert settings.persistence.idmouse.bearer_token() == "local-bearer-token"


def test_settings_listen_option(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        listen = "127.0.0.1"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.listen == "127.0.0.1"


def test_settings_host_option_is_deprecated(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        host = "127.0.0.1"
        """
    )

    with caplog.at_level("WARNING"):
        settings = Settings.from_toml(config)

    assert settings.listen == "127.0.0.1"
    assert any("deprecated" in record.message for record in caplog.records)


def test_settings_listen_takes_precedence_over_host(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        host = "10.0.0.1"
        listen = "127.0.0.1"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.listen == "127.0.0.1"


def test_settings_parse_in_memory_persistence_without_surrealdb_config(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        backend = "in-memory"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.persistence is not None
    assert settings.persistence.backend == "in-memory"
    assert settings.persistence.uri is None
    assert settings.persistence.idmouse is None


def test_settings_parse_dynamodb_config(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        backend = "dynamodb"
        table-name = "relcoord-image-versions"
        region-name = "eu-west-2"
        endpoint-url = "http://localhost:8000"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.persistence is not None
    assert settings.persistence.backend == "dynamodb"
    assert settings.persistence.table_name == "relcoord-image-versions"
    assert settings.persistence.region_name == "eu-west-2"
    assert settings.persistence.endpoint_url == "http://localhost:8000"


def test_settings_rejects_dynamodb_without_table_name(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        backend = "dynamodb"
        """
    )

    with pytest.raises(
        ValueError, match="persistence.table-name must be a non-empty string"
    ):
        Settings.from_toml(config)


def test_settings_rejects_unknown_persistence_backend(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        backend = "postgres"
        """
    )

    with pytest.raises(
        ValueError,
        match=(
            "persistence.backend must be one of 'in-memory', 'surrealdb', or 'dynamodb'"
        ),
    ):
        Settings.from_toml(config)


def test_settings_parses_manifests_repository(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        manifests-repository = "https://github.com/acme/manifests.git"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.manifests_repository == "https://github.com/acme/manifests.git"


def test_settings_parses_system_repository(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        system-repository = "https://github.com/acme/system.git"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.system_repository == "https://github.com/acme/system.git"


def test_settings_defaults_system_repository_to_none(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text("")

    assert Settings.from_toml(config).system_repository is None


def test_settings_rejects_empty_system_repository(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('system-repository = ""\n')

    with pytest.raises(
        ValueError, match="system-repository must be a non-empty string"
    ):
        Settings.from_toml(config)


def test_settings_parses_output_entries(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/example/manifests"
        directory = "example-dev"

        [output.vars]
        cluster_name = "example-dev"
        account_id = 111122223333
        issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLEDEVCLUSTERID"

        [[output]]
        name = "example-prod"
        repository = "https://github.com/example/manifests"
        directory = "example-prod"

        [output.vars]
        cluster_name = "example-prod"
        account_id = 444455556666
        issuer = "https://oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLEPRODCLUSTERID"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.outputs[0].name == "example-dev"
    assert settings.outputs[0].repository == "https://github.com/example/manifests"
    assert settings.outputs[0].directory == Path("example-dev")
    assert settings.outputs[0].vars == {
        "cluster_name": "example-dev",
        "account_id": 111122223333,
        "issuer": "https://oidc.eks.eu-west-1.amazonaws.com/id/EXAMPLEDEVCLUSTERID",
    }
    assert settings.outputs[1].name == "example-prod"
    assert settings.outputs[1].directory == Path("example-prod")
    assert settings.outputs[1].vars["cluster_name"] == "example-prod"


def test_settings_parses_output_target(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/example/manifests"
        target = "dev"

        [[output]]
        name = "example-prod"
        repository = "https://github.com/example/manifests"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.outputs[0].target == "dev"
    assert settings.outputs[0].target_name == "dev"
    assert settings.outputs[1].target is None
    assert settings.outputs[1].target_name == "example-prod"


def test_settings_rejects_empty_output_target(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/example/manifests"
        target = ""
        """
    )

    with pytest.raises(ValueError, match="output.target must be a non-empty string"):
        Settings.from_toml(config)


def test_settings_defaults_output_directory_to_repository_root(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/example/manifests"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.outputs[0].directory == Path(".")


def test_settings_rejects_diff_output(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        diff-output = "example-dev"

        [[output]]
        name = "example-dev"
        repository = "https://github.com/example/manifests"
        directory = "example-dev"

        [[output]]
        name = "example-prod"
        repository = "https://github.com/example/manifests"
        directory = "example-prod"
        """
    )

    with pytest.raises(
        ValueError,
        match=(
            "diff-output is not supported; a diff reports every output the "
            "commit affects"
        ),
    ):
        Settings.from_toml(config)


def test_settings_rejects_output_with_non_scalar_vars(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        directory = "example-dev"

        [output.vars]
        cluster_names = ["example-dev"]
        """
    )

    with pytest.raises(
        TypeError,
        match="output.vars.cluster_names must be a string, number, or boolean",
    ):
        Settings.from_toml(config)


def test_settings_rejects_output_with_parent_directory(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        directory = "../example-dev"
        """
    )

    with pytest.raises(
        ValueError, match="output.directory must be a relative path without '..'"
    ):
        Settings.from_toml(config)


def test_settings_rejects_manifests_repository_and_outputs(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        manifests-repository = "https://github.com/acme/manifests.git"

        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        directory = "example-dev"
        """
    )

    with pytest.raises(
        ValueError, match=r"configure either manifests-repository or \[\[output\]\]"
    ):
        Settings.from_toml(config)


def test_settings_parses_detect_deployment(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        detect-deployment = true

        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        connection-type = "eks"
        api-endpoint = "https://example.eks.amazonaws.com"
        ca-path = "/etc/relcoord/example-dev-ca.pem"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.detect_deployment is True
    assert settings.outputs[0].connection_type == "eks"


def test_settings_defaults_detect_deployment_to_false(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text("")

    settings = Settings.from_toml(config)

    assert settings.detect_deployment is False


def test_settings_defaults_log_level_to_info(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text("")

    settings = Settings.from_toml(config)

    assert settings.log_level == "INFO"


def test_settings_parses_log_level(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('log-level = "debug"\n')

    settings = Settings.from_toml(config)

    assert settings.log_level == "DEBUG"


def test_settings_rejects_unknown_log_level(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('log-level = "verbose"\n')

    with pytest.raises(
        ValueError,
        match="log-level must be one of DEBUG, INFO, WARNING, ERROR, or CRITICAL",
    ):
        Settings.from_toml(config)


def test_settings_rejects_non_boolean_detect_deployment(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('detect-deployment = "yes"\n')

    with pytest.raises(TypeError, match="detect-deployment must be a boolean"):
        Settings.from_toml(config)


def test_settings_rejects_empty_manifests_repository(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('manifests-repository = ""\n')

    with pytest.raises(
        ValueError, match="manifests-repository must be a non-empty string"
    ):
        Settings.from_toml(config)


def test_settings_rejects_old_idmouse_token_path_key(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idmouse-token"
    token_file.write_text("local-bearer-token\n")
    config.write_text(
        f"""
        [persistence]
        uri = "ws://surrealdb:8000/"

        [persistence.idmouse]
        url = "http://idmouse:9000/token"
        token_path = "{token_file}"
        """
    )

    with pytest.raises(
        ValueError, match="persistence.idmouse.token-path must be a non-empty string"
    ):
        Settings.from_toml(config)


def test_settings_parses_role_entries(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[role]]
        name = "kubernetes-default"
        audience = "relcoord"
        issuer = "https://kubernetes.default.svc"
        jwks-uri = "https://kubernetes.default.svc/openid/v1/jwks"

        [role.claims]
        sub = "system:serviceaccount:default:default"

        [[role]]
        name = "buildkite"
        audience = "relcoord"
        issuer = "https://agent.buildkite.com"
        jwks-uri = "https://agent.buildkite.com/.well-known/jwks"
        algorithms = ["RS256", "ES256"]
        """
    )

    settings = Settings.from_toml(config)

    assert len(settings.roles) == 2
    assert settings.roles[0].name == "kubernetes-default"
    assert settings.roles[0].jwks_uri == "https://kubernetes.default.svc/openid/v1/jwks"
    assert settings.roles[0].claims == {"sub": "system:serviceaccount:default:default"}
    assert not hasattr(settings.roles[1], "algorithms")


def test_settings_parses_idcat_config(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    config.write_text(
        f"""
        [idcat]
        endpoint = "https://idcat.example.test/base"
        github-app = "deployments"
        token-path = "{token_file}"
        """
    )

    settings = Settings.from_toml(config)

    assert settings.idcat is not None
    assert settings.idcat.endpoint == "https://idcat.example.test/base"
    assert settings.idcat.github_app == "deployments"
    assert settings.idcat.bearer_token() == "idcat-bearer-token"


def test_settings_rejects_old_idcat_github_app_key(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    config.write_text(
        f"""
        [idcat]
        endpoint = "https://idcat.example.test/base"
        github_app = "deployments"
        token-path = "{token_file}"
        """
    )

    with pytest.raises(ValueError, match="idcat.github-app must be a non-empty string"):
        Settings.from_toml(config)


def test_settings_rejects_old_idcat_endpoint_key(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    token_file = tmp_path / "idcat-token"
    token_file.write_text("idcat-bearer-token\n")
    config.write_text(
        f"""
        [idcat]
        idcat-endpoint = "https://idcat.example.test/base"
        github-app = "deployments"
        token-path = "{token_file}"
        """
    )

    with pytest.raises(ValueError, match="idcat.endpoint must be a non-empty string"):
        Settings.from_toml(config)


def test_settings_allows_role_without_explicit_jwks_uri(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[role]]
        name = "kubernetes-default"
        audience = "relcoord"
        issuer = "https://kubernetes.default.svc"
        """
    )

    settings = Settings.from_toml(config)

    assert len(settings.roles) == 1
    assert settings.roles[0].jwks_uri is None


def test_settings_rejects_duplicate_role_names(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[role]]
        name = "x"
        audience = "relcoord"
        issuer = "https://issuer"
        jwks-uri = "https://issuer/.well-known/jwks.json"

        [[role]]
        name = "x"
        audience = "relcoord"
        issuer = "https://issuer"
        jwks-uri = "https://issuer/.well-known/jwks.json"
        """
    )

    with pytest.raises(ValueError, match="duplicate role 'x'"):
        Settings.from_toml(config)


def test_settings_explains_multiline_inline_table_parse_errors(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        uri = "ws://surrealdb:8000/"
        idmouse = {
            url = "http://idmouse:9000/token",
            token-path = "/tmp/idmouse-token"
        }
        """
    )

    with pytest.raises(ValueError, match=r"tomllib parses TOML 1\.0\.0"):
        Settings.from_toml(config)


def test_settings_rejects_persistence_without_idmouse(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [persistence]
        uri = "ws://surrealdb:8000/"
        """
    )

    with pytest.raises(ValueError, match="persistence.idmouse must be configured"):
        Settings.from_toml(config)


def test_settings_parses_an_eks_connection_on_an_output(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        connection-type = "eks"
        api-endpoint = "https://example.eks.amazonaws.com"
        ca-path = "/etc/relcoord/example-dev-ca.pem"
        region = "eu-west-1"
        eks-cluster-name = "example-dev-eks"
        """
    )

    output = Settings.from_toml(config).outputs[0]

    assert output.connection_type == "eks"
    assert output.api_endpoint == "https://example.eks.amazonaws.com"
    assert output.ca_path == Path("/etc/relcoord/example-dev-ca.pem")
    assert output.region == "eu-west-1"
    assert output.eks_name == "example-dev-eks"


def test_settings_parses_a_local_connection_with_in_cluster_defaults(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "local"
        repository = "https://github.com/acme/manifests"
        connection-type = "local"
        """
    )

    output = Settings.from_toml(config).outputs[0]

    assert output.connection_type == "local"
    assert output.api_endpoint == KUBERNETES_SERVICE_HOST
    assert output.ca_path == KUBERNETES_CA_CERT_PATH


def test_settings_rejects_an_unknown_output_connection_type(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        connection-type = "kubeconfig"
        """
    )

    with pytest.raises(
        ValueError, match="output.connection-type must be 'eks' or 'local'"
    ):
        Settings.from_toml(config)


def test_settings_requires_connection_type_with_connection_properties(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        api-endpoint = "https://example.eks.amazonaws.com"
        ca-path = "/ca.pem"
        """
    )

    with pytest.raises(ValueError, match="output.connection-type must be set"):
        Settings.from_toml(config)


def test_settings_rejects_an_eks_output_without_an_api_endpoint(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        connection-type = "eks"
        ca-path = "/ca.pem"
        """
    )

    with pytest.raises(ValueError, match="output.api-endpoint must be"):
        Settings.from_toml(config)


def test_settings_rejects_a_standalone_cluster_block(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[cluster]]
        name = "example-dev"
        connection-type = "eks"
        api-endpoint = "https://example.eks.amazonaws.com"
        ca-path = "/ca.pem"
        """
    )

    with pytest.raises(ValueError, match=r"\[\[cluster\]\] entries are not supported"):
        Settings.from_toml(config)


def test_settings_rejects_an_output_cluster_reference(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        cluster = "example-dev"
        """
    )

    with pytest.raises(ValueError, match="output.cluster is not supported"):
        Settings.from_toml(config)


def test_settings_rejects_detect_deployment_without_connection_type_per_output(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        detect-deployment = true

        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        connection-type = "local"

        [[output]]
        name = "example-prod"
        repository = "https://github.com/acme/manifests"
        """
    )

    with pytest.raises(ValueError, match="every output to set connection-type"):
        Settings.from_toml(config)


def test_settings_rejects_detect_deployment_with_manifests_repository(
    tmp_path: Path,
) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        detect-deployment = true
        manifests-repository = "https://github.com/acme/manifests"
        """
    )

    with pytest.raises(ValueError, match="detect-deployment requires"):
        Settings.from_toml(config)


ROLLOUT_OUTPUTS = """
detect-deployment = true

[[output]]
name = "platform-dev"
repository = "https://github.com/acme/manifests"
connection-type = "local"

[[output]]
name = "platform-prod"
repository = "https://github.com/acme/manifests"
connection-type = "local"

[[output]]
name = "observability"
repository = "https://github.com/acme/manifests"
connection-type = "local"
"""


def _rollout_config(tmp_path: Path, rollouts: str) -> Path:
    config = tmp_path / "relcoord.toml"
    config.write_text(ROLLOUT_OUTPUTS + rollouts)
    return config


def test_settings_parse_rollout_stages(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["platform-prod", "observability"]
        """,
    )

    settings = Settings.from_toml(config)

    assert [rollout.name for rollout in settings.rollouts] == ["linear"]
    rollout = settings.rollouts[0]
    assert [stage.outputs for stage in rollout.stages] == [
        ("platform-dev",),
        ("platform-prod", "observability"),
    ]
    assert rollout.output_names == (
        "platform-dev",
        "platform-prod",
        "observability",
    )


def test_settings_default_to_no_rollouts(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "example-dev"
        repository = "https://github.com/acme/manifests"
        """
    )

    assert Settings.from_toml(config).rollouts == []


def test_settings_reject_rollout_without_detect_deployment(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        [[output]]
        name = "platform-dev"
        repository = "https://github.com/acme/manifests"

        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]
        """
    )

    with pytest.raises(ValueError, match=r"\[\[rollout\]\] requires detect-deployment"):
        Settings.from_toml(config)


def test_settings_reject_rollout_without_outputs(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text(
        """
        detect-deployment = true
        manifests-repository = "https://github.com/acme/manifests"

        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["manifests"]
        """
    )

    with pytest.raises(
        ValueError, match=r"\[\[rollout\]\] requires \[\[output\]\] entries"
    ):
        Settings.from_toml(config)


def test_settings_reject_rollout_naming_an_unconfigured_output(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["platform-prod", "observability", "platform-staging"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=(
            "rollout 'linear' names output 'platform-staging', which is not "
            "configured; expected one of observability, platform-dev, platform-prod"
        ),
    ):
        Settings.from_toml(config)


def test_settings_reject_an_output_in_two_stages_of_one_rollout(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["platform-dev", "platform-prod", "observability"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=(
            "output 'platform-dev' appears in more than one stage of rollout 'linear'"
        ),
    ):
        Settings.from_toml(config)


def test_settings_reject_an_output_in_two_rollouts(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["platform-prod"]

        [[rollout]]
        name = "observability"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["observability"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=(
            "output 'platform-dev' appears in more than one rollout stage; "
            "also in rollout 'linear'"
        ),
    ):
        Settings.from_toml(config)


def test_settings_reject_an_output_outside_every_rollout(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev"]

        [[rollout.stage]]
        outputs = ["platform-prod"]
        """,
    )

    with pytest.raises(
        ValueError,
        match=(
            "every output must appear in a rollout stage once rollouts are "
            "configured; observability does not"
        ),
    ):
        Settings.from_toml(config)


def test_settings_reject_duplicate_rollout_names(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["platform-dev", "platform-prod"]

        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        outputs = ["observability"]
        """,
    )

    with pytest.raises(ValueError, match="duplicate rollout 'linear'"):
        Settings.from_toml(config)


def test_settings_reject_a_rollout_without_a_name(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]

        [[rollout.stage]]
        outputs = ["platform-dev", "platform-prod", "observability"]
        """,
    )

    with pytest.raises(ValueError, match="rollout.name must be a non-empty string"):
        Settings.from_toml(config)


def test_settings_reject_a_rollout_without_stages(tmp_path: Path) -> None:
    config = _rollout_config(
        tmp_path,
        """
        [[rollout]]
        name = "linear"
        """,
    )

    with pytest.raises(
        ValueError,
        match=r"rollout 'linear' must declare at least one \[\[rollout.stage\]\]",
    ):
        Settings.from_toml(config)


@pytest.mark.parametrize(
    "outputs",
    [
        pytest.param("outputs = []", id="empty"),
        pytest.param('outputs = "platform-dev"', id="string"),
        pytest.param("outputs = [1]", id="not-a-name"),
        pytest.param("", id="missing"),
    ],
)
def test_settings_reject_a_stage_without_output_names(
    tmp_path: Path, outputs: str
) -> None:
    config = _rollout_config(
        tmp_path,
        f"""
        [[rollout]]
        name = "linear"

        [[rollout.stage]]
        {outputs}
        """,
    )

    with pytest.raises(
        ValueError,
        match=(
            "rollout 'linear' stage 1: outputs must be a non-empty array of "
            "output names"
        ),
    ):
        Settings.from_toml(config)


def test_settings_reject_a_rollout_that_is_not_a_table(tmp_path: Path) -> None:
    config = tmp_path / "relcoord.toml"
    config.write_text('rollout = ["linear"]\n' + ROLLOUT_OUTPUTS)

    with pytest.raises(TypeError, match="each rollout entry must be a table"):
        Settings.from_toml(config)
