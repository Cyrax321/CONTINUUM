"""Unit tests for DependencyGraph in continuum.analysis.depends (issue #2010)."""

from __future__ import annotations

from pathlib import Path

import pytest

from continuum.analysis.depends import DependencyGraph


def test_is_stdlib_identifies_standard_and_third_party_modules() -> None:
    assert DependencyGraph.is_stdlib("json") is True
    assert DependencyGraph.is_stdlib("sys") is True
    assert DependencyGraph.is_stdlib("pathlib") is True
    assert DependencyGraph.is_stdlib("ast") is True
    assert DependencyGraph.is_stdlib("os.path") is True

    assert DependencyGraph.is_stdlib("pytest") is False
    assert DependencyGraph.is_stdlib("pydantic") is False
    assert DependencyGraph.is_stdlib("flask") is False


def test_third_party_imports_filters_stdlib_and_declared_and_resolves_paths(
    tmp_path: Path,
) -> None:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\ndependencies = ["requests>=2.31.0"]\n',
        encoding="utf-8",
    )
    app_py = tmp_path / "pkg" / "app.py"
    app_py.parent.mkdir(parents=True)
    app_py.write_text(
        "import json\nimport os.path\nimport requests\nimport flask\nfrom celery.app import task\n",
        encoding="utf-8",
    )

    graph = DependencyGraph(tmp_path)

    # Relative path (resolved via self.root / target), direct Path, and resolved Path
    assert graph.third_party_imports("pkg/app.py") == {"flask", "celery"}
    assert graph.third_party_imports(app_py) == {"flask", "celery"}
    assert graph.third_party_imports(app_py.resolve()) == {"flask", "celery"}
    assert graph.third_party_imports("pkg/missing.py") == set()

    assert graph.owner_of("pkg/app.py") == {"requests"}
    assert graph.owner_of("pkg/missing.py") == set()


def test_requirements_txt_discovery_ignores_comments_blanks_and_options(
    tmp_path: Path,
) -> None:
    (tmp_path / "requirements.txt").write_text(
        "\n"
        "# Core dependencies\n"
        "-r base.txt\n"
        "--index-url https://pypi.org/simple\n"
        "requests==2.31.0\n"
        "PyYAML>=6.0 ; python_version >= '3.10'\n"
        "Pillow[extra]~=10.0\n",
        encoding="utf-8",
    )
    worker_py = tmp_path / "worker.py"
    worker_py.write_text(
        "import requests\nimport yaml\nfrom PIL import Image\nimport httpx\n",
        encoding="utf-8",
    )

    graph = DependencyGraph(tmp_path)
    assert graph.declared == {"requests", "pyyaml", "pillow"}
    assert graph.owner_of("worker.py") == {"requests", "pyyaml", "pillow"}
    assert graph.third_party_imports("worker.py") == {"httpx"}


def test_custom_aliases_mapping_supports_string_and_iterable_targets(
    tmp_path: Path,
) -> None:
    mod_file = tmp_path / "service.py"
    mod_file.write_text(
        "import mymod\nfrom alt_mod.sub import helper\nimport undeclared_pkg\n",
        encoding="utf-8",
    )

    graph = DependencyGraph(
        tmp_path,
        requirements=["my-dist>=1.0", "multi-dist==2.0"],
        aliases={
            "my-dist": "mymod",
            "multi-dist": ["alt_mod", "other_mod"],
        },
    )

    assert graph.owner_of(mod_file) == {"my-dist", "multi-dist"}
    assert graph.files_using("my-dist") == {mod_file}
    assert graph.files_using("multi-dist") == {mod_file}
    assert graph.third_party_imports(mod_file) == {"undeclared_pkg"}


def test_google_namespace_and_cloud_version_suffix_normalization(
    tmp_path: Path,
) -> None:
    gcp_file = tmp_path / "gcp.py"
    gcp_file.write_text(
        "from google import cloud\n"
        "from google.cloud import storage\n"
        "from google.cloud import pubsub_v1\n"
        "from google import protobuf\n"
        "import google.cloud.secretmanager_v1beta1\n"
        "from google.cloud.bigquery_v2 import Client\n",
        encoding="utf-8",
    )

    graph = DependencyGraph(
        tmp_path,
        requirements=[
            "google-cloud",
            "google-cloud-storage",
            "google-cloud-pubsub",
            "protobuf",
            "google-cloud-secret-manager",
        ],
    )

    owners = graph.owner_of(gcp_file)
    assert owners == {
        "google-cloud",
        "google-cloud-storage",
        "google-cloud-pubsub",
        "protobuf",
        "google-cloud-secret-manager",
    }
    assert graph.files_using("google-cloud") == {gcp_file}
    assert graph.files_using("google-cloud-pubsub") == {gcp_file}
    assert graph.files_using("protobuf") == {gcp_file}
    assert graph.third_party_imports(gcp_file) == {"google.cloud.bigquery"}


def test_scan_degrades_gracefully_on_syntax_and_decode_errors_and_bad_pyproject(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Malformed pyproject.toml should be ignored without raising
    (tmp_path / "pyproject.toml").write_text("not = [valid toml", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    # Broken Python syntax
    (tmp_path / "broken_syntax.py").write_text("def oops(:\n", encoding="utf-8")
    # Invalid UTF-8 bytes
    (tmp_path / "bad_encoding.py").write_bytes(b"\xff\xfe\x00\x00invalid utf-8")
    # Excluded directory (.venv) should be skipped
    venv_file = tmp_path / ".venv" / "ignored.py"
    venv_file.parent.mkdir(parents=True)
    venv_file.write_text("import requests\n", encoding="utf-8")

    # Valid file
    valid_file = tmp_path / "valid.py"
    valid_file.write_text("import requests\n", encoding="utf-8")

    graph = DependencyGraph(tmp_path)
    assert graph.owner_of(valid_file) == {"requests"}
    assert graph.owner_of(tmp_path / "broken_syntax.py") == set()
    assert graph.owner_of(tmp_path / "bad_encoding.py") == set()
    assert graph.files_using("requests") == {valid_file}

    # Also verify graceful degradation when Path.rglob raises OSError
    def _raise_oserror(self: Path, pattern: str) -> list[Path]:
        raise OSError("simulated permission error")

    monkeypatch.setattr(Path, "rglob", _raise_oserror)
    empty_graph = DependencyGraph(tmp_path, requirements=["requests"])
    assert empty_graph.files_using("requests") == set()
