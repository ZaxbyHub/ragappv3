#!/usr/bin/env python3
"""Check high-value environment/config contracts across repo surfaces."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def read(relative_path: str) -> str:
    return (ROOT / relative_path).read_text(encoding="utf-8")


def fail(message: str) -> None:
    print(f"config-contract: {message}", file=sys.stderr)


def env_value(env_text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}=(.*)$", env_text, re.MULTILINE)
    return match.group(1).strip() if match else None


def compose_default(compose_text: str, name: str) -> str | None:
    match = re.search(rf"\${{\s*{re.escape(name)}:-([^}}]*)}}", compose_text)
    return match.group(1).strip() if match else None


def parse_origins(value: str | None) -> list[str]:
    if value is None:
        return []
    return [origin.strip() for origin in value.split(",") if origin.strip()]


def backend_cors_default(config_text: str) -> list[str]:
    match = re.search(
        r"backend_cors_origins:.*?=\s*\[(?P<body>.*?)\]",
        config_text,
        re.DOTALL,
    )
    if not match:
        return []
    return re.findall(r'"([^"]+)"', match.group("body"))


def backend_int_default(config_text: str, field_name: str) -> str | None:
    match = re.search(
        rf"{re.escape(field_name)}:\s*int\s*=\s*(\d+)",
        config_text,
    )
    return match.group(1) if match else None


def schema_parser_limit_mb(schema_parser_text: str) -> str | None:
    match = re.search(
        r"MAX_FILE_SIZE\s*=\s*(\d+)\s*\*\s*1024\s*\*\s*1024",
        schema_parser_text,
    )
    return match.group(1) if match else None


def frontend_upload_limit_mb(upload_limits_text: str) -> str | None:
    match = re.search(
        r"MAX_UPLOAD_FILE_SIZE_MB\s*=\s*(\d+)",
        upload_limits_text,
    )
    return match.group(1) if match else None


def nginx_client_max_body_sizes_mb(docs_text: str) -> list[int]:
    return [
        int(value)
        for value in re.findall(r"client_max_body_size\s+(\d+)m;", docs_text)
    ]


def contains_all(text: str, needles: list[str]) -> bool:
    return all(needle in text for needle in needles)


def main() -> int:
    failures: list[str] = []

    env_text = read(".env.example")
    compose_text = read("docker-compose.yml")
    backend_config = read("backend/app/config.py")
    root_dockerfile = read("Dockerfile")
    frontend_dockerfile = read("frontend/Dockerfile")
    frontend_api = read("frontend/src/lib/api.ts")
    frontend_upload_limits = read("frontend/src/lib/uploadLimits.ts")
    frontend_paths = read("frontend/src/lib/paths.ts")
    vite_config = read("frontend/vite.config.ts")
    schema_parser = read("backend/app/services/schema_parser.py")
    release_docs = read("docs/release.md")
    docs_text = read("INSTALLATION.md") + "\n" + read("docs/admin-guide.md")

    backend_default = backend_cors_default(backend_config)
    env_default = parse_origins(env_value(env_text, "BACKEND_CORS_ORIGINS"))
    compose_cors_default = parse_origins(
        compose_default(compose_text, "BACKEND_CORS_ORIGINS")
    )
    if not backend_default:
        failures.append("backend/app/config.py CORS default could not be parsed")
    if env_default != backend_default:
        failures.append(
            ".env.example BACKEND_CORS_ORIGINS default "
            f"{env_default!r} does not match backend default {backend_default!r}"
        )
    if compose_cors_default != backend_default:
        failures.append(
            "docker-compose.yml BACKEND_CORS_ORIGINS default "
            f"{compose_cors_default!r} does not match backend default {backend_default!r}"
        )

    upload_default = backend_int_default(backend_config, "max_file_size_mb")
    if upload_default is None:
        failures.append("backend/app/config.py max_file_size_mb default could not be parsed")
    if env_value(env_text, "MAX_FILE_SIZE_MB") != upload_default:
        failures.append(
            ".env.example MAX_FILE_SIZE_MB default "
            f"{env_value(env_text, 'MAX_FILE_SIZE_MB')!r} does not match backend default {upload_default!r}"
        )
    if compose_default(compose_text, "MAX_FILE_SIZE_MB") != upload_default:
        failures.append(
            "docker-compose.yml MAX_FILE_SIZE_MB default "
            f"{compose_default(compose_text, 'MAX_FILE_SIZE_MB')!r} does not match backend default {upload_default!r}"
        )
    if schema_parser_limit_mb(schema_parser) != upload_default:
        failures.append(
            "backend/app/services/schema_parser.py MAX_FILE_SIZE "
            f"{schema_parser_limit_mb(schema_parser)!r} does not match backend default {upload_default!r}"
        )
    if frontend_upload_limit_mb(frontend_upload_limits) != upload_default:
        failures.append(
            "frontend/src/lib/uploadLimits.ts MAX_UPLOAD_FILE_SIZE_MB "
            f"{frontend_upload_limit_mb(frontend_upload_limits)!r} does not match backend default {upload_default!r}"
        )

    required_env = [
        "APP_ROOT_PATH",
        "VITE_APP_BASENAME",
        "VITE_API_URL",
        "FORWARDED_ALLOW_IPS",
        "BACKEND_CORS_ORIGINS",
        "MAX_FILE_SIZE_MB",
    ]
    for name in required_env:
        if env_value(env_text, name) is None:
            failures.append(f".env.example is missing {name}")

    compose_required = [
        "APP_ROOT_PATH",
        "VITE_APP_BASENAME",
        "VITE_API_URL",
        "FORWARDED_ALLOW_IPS",
        "BACKEND_CORS_ORIGINS",
        "MAX_FILE_SIZE_MB",
    ]
    for name in compose_required:
        if name not in compose_text:
            failures.append(f"docker-compose.yml is missing {name}")

    if env_value(env_text, "APP_ROOT_PATH") != "":
        failures.append(".env.example APP_ROOT_PATH should default to empty root path")
    if env_value(env_text, "VITE_APP_BASENAME") != "/":
        failures.append(".env.example VITE_APP_BASENAME should default to /")
    if env_value(env_text, "VITE_API_URL") != "":
        failures.append(".env.example VITE_API_URL should default to empty (derived from VITE_APP_BASENAME)")
    if compose_default(compose_text, "APP_ROOT_PATH") != "":
        failures.append("docker-compose.yml APP_ROOT_PATH should default to empty")
    if compose_default(compose_text, "VITE_APP_BASENAME") != "/":
        failures.append("docker-compose.yml VITE_APP_BASENAME should default to /")
    if compose_default(compose_text, "VITE_API_URL") != "":
        failures.append("docker-compose.yml VITE_API_URL should default to empty (derived from VITE_APP_BASENAME)")

    prefix_example = [
        "APP_ROOT_PATH=/knowledgevault",
        "VITE_APP_BASENAME=/knowledgevault",
        "VITE_API_URL=/knowledgevault/api",
    ]
    if not contains_all(env_text, prefix_example):
        failures.append(".env.example is missing the coordinated /knowledgevault example")
    if not contains_all(docs_text, prefix_example):
        failures.append("docs are missing the coordinated /knowledgevault example")
    if upload_default is not None:
        upload_default_mb = int(upload_default)
        required_nginx_mb = (upload_default_mb * 125 + 99) // 100
        nginx_limits = nginx_client_max_body_sizes_mb(docs_text)
        if not nginx_limits or max(nginx_limits) < required_nginx_mb:
            failures.append(
                "docs are missing nginx client_max_body_size guidance "
                f">= {required_nginx_mb}m for {upload_default_mb} MB uploads"
            )
        release_default = f"default: {upload_default_mb}MB"
        if release_default not in release_docs:
            failures.append(
                "docs/release.md troubleshooting default "
                f"does not mention {release_default!r}"
            )

    for dockerfile_name, dockerfile_text in [
        ("Dockerfile", root_dockerfile),
        ("frontend/Dockerfile", frontend_dockerfile),
    ]:
        for token in [
            "ARG VITE_APP_BASENAME=/",
            "ARG VITE_API_URL=",
            "ENV VITE_APP_BASENAME=${VITE_APP_BASENAME}",
            "ENV VITE_API_URL=${VITE_API_URL}",
        ]:
            if token not in dockerfile_text:
                failures.append(f"{dockerfile_name} is missing {token}")

    if 'import.meta.env.VITE_API_URL || appPath("/api")' not in frontend_api:
        failures.append("frontend API default no longer derives from appPath when VITE_API_URL is empty")
    if "VITE_APP_BASENAME" not in frontend_paths or "BASE_URL" not in frontend_paths:
        failures.append("frontend path helper no longer reads VITE_APP_BASENAME/BASE_URL")
    if "normalizeViteBase(appBasename)" not in vite_config:
        failures.append("Vite config no longer derives base from normalized basename")

    for message in failures:
        fail(message)
    if failures:
        return 1
    print("config-contract: all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
