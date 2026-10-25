"""Tests for workload identity attestation binding and SPIFFE integration (issue #745)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from continuum.security.attestation import (
    generate_keypair,
    sign_chain,
    verify_attestation,
)
from continuum.security.workload_identity import (
    FakeWorkloadIdentityProvider,
    SpiffeIdentityProvider,
    WorkloadIdentity,
    WorkloadIdentityUnavailableError,
    resolve_workload_provider,
)


def test_workload_identity_model_validation() -> None:
    # Valid SPIFFE ID
    ident = WorkloadIdentity("spiffe://example.org/ns/prod/sa/worker")
    assert ident.spiffe_id == "spiffe://example.org/ns/prod/sa/worker"
    assert ident.trust_domain == "example.org"
    assert ident.provider == "spiffe"

    # Custom attributes and provider
    ident2 = WorkloadIdentity(
        "spiffe://acme.corp/service/api",
        provider="custom",
        attributes={"cluster": "us-east-1"},
    )
    assert ident2.provider == "custom"
    assert ident2.attributes == {"cluster": "us-east-1"}

    # Serialisation round-trip
    d = ident2.to_dict()
    restored = WorkloadIdentity.from_dict(d)
    assert restored.spiffe_id == ident2.spiffe_id
    assert restored.provider == ident2.provider
    assert restored.trust_domain == ident2.trust_domain
    assert restored.attributes == ident2.attributes

    # Invalid SPIFFE IDs
    with pytest.raises(ValueError, match="must begin with 'spiffe://'"):
        WorkloadIdentity("https://example.org/not-spiffe")
    with pytest.raises(ValueError, match="missing trust domain"):
        WorkloadIdentity("spiffe://")
    with pytest.raises(ValueError, match="must be a non-empty string"):
        WorkloadIdentity("")


def test_fake_workload_provider_behavior() -> None:
    provider = FakeWorkloadIdentityProvider("spiffe://example.org/workload/test")
    ident = provider.get_identity()
    assert ident.spiffe_id == "spiffe://example.org/workload/test"
    assert ident.provider == "fake"
    assert provider.verify_identity(ident) is True

    # Mismatched identity
    other = WorkloadIdentity("spiffe://example.org/workload/other")
    assert provider.verify_identity(other) is False

    # Invalid provider mode
    invalid_provider = FakeWorkloadIdentityProvider(valid=False)
    assert invalid_provider.verify_identity(ident) is False

    # Unavailable provider mode
    unavail = FakeWorkloadIdentityProvider(available=False)
    with pytest.raises(WorkloadIdentityUnavailableError, match="unavailable"):
        unavail.get_identity()


def test_spiffe_provider_unavailable_without_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("SPIFFE_ENDPOINT_SOCKET", raising=False)
    provider = SpiffeIdentityProvider()
    with pytest.raises(WorkloadIdentityUnavailableError, match="no SPIFFE Workload API socket"):
        provider.get_identity()


def test_spiffe_provider_verification() -> None:
    provider = SpiffeIdentityProvider(trust_domain="example.org")
    good = WorkloadIdentity("spiffe://example.org/sa/agent")
    bad_domain = WorkloadIdentity("spiffe://evil.com/sa/agent")
    assert provider.verify_identity(good) is True
    assert provider.verify_identity(bad_domain) is False


def test_resolve_workload_provider() -> None:
    fake = resolve_workload_provider("fake", spiffe_id="spiffe://test.org/app")
    assert isinstance(fake, FakeWorkloadIdentityProvider)
    assert fake.get_identity().spiffe_id == "spiffe://test.org/app"

    spiffe = resolve_workload_provider("spiffe")
    assert isinstance(spiffe, SpiffeIdentityProvider)

    with pytest.raises(ValueError, match="unknown workload identity provider"):
        resolve_workload_provider("unsupported")


def test_identity_bound_attestation_round_trip() -> None:
    priv_pem, pub_pem = generate_keypair()
    ident = WorkloadIdentity("spiffe://example.org/ns/prod/sa/reconciliation-agent")

    attest = sign_chain(
        priv_pem,
        run_id="run_spiffe_1",
        trusted_through_seq=10,
        chain_hash="hash_root_123",
        signer="prod-orchestrator",
        workload_identity=ident,
    )
    assert attest.workload_identity is not None
    assert (
        attest.workload_identity["spiffe_id"]
        == "spiffe://example.org/ns/prod/sa/reconciliation-agent"
    )

    doc = attest.to_dict()
    assert "workload_identity" in doc

    # Verification with matching identity
    assert verify_attestation(doc, expected_chain_hash="hash_root_123") is True
    assert (
        verify_attestation(
            doc,
            expected_chain_hash="hash_root_123",
            expected_workload_identity="spiffe://example.org/ns/prod/sa/reconciliation-agent",
        )
        is True
    )

    # Verification with provider interface
    provider = FakeWorkloadIdentityProvider("spiffe://example.org/ns/prod/sa/reconciliation-agent")
    assert (
        verify_attestation(
            doc,
            expected_chain_hash="hash_root_123",
            identity_provider=provider,
        )
        is True
    )


def test_identity_bound_attestation_rejects_identity_mismatch() -> None:
    priv_pem, _ = generate_keypair()
    ident = WorkloadIdentity("spiffe://example.org/workload/legit")

    attest = sign_chain(
        priv_pem,
        run_id="run_1",
        trusted_through_seq=5,
        chain_hash="hash_abc",
        workload_identity=ident,
    )

    # Mismatched expected identity
    assert (
        verify_attestation(
            attest,
            expected_workload_identity="spiffe://example.org/workload/impostor",
        )
        is False
    )

    # Provider rejecting identity
    other_provider = FakeWorkloadIdentityProvider("spiffe://example.org/workload/other")
    assert (
        verify_attestation(
            attest,
            identity_provider=other_provider,
        )
        is False
    )


def test_identity_bound_attestation_rejects_tampered_identity() -> None:
    priv_pem, _ = generate_keypair()
    ident = WorkloadIdentity("spiffe://example.org/workload/original")

    attest = sign_chain(
        priv_pem,
        run_id="run_1",
        trusted_through_seq=5,
        chain_hash="hash_abc",
        workload_identity=ident,
    )
    doc = attest.to_dict()

    # Tampering with the embedded workload_identity breaks the Ed25519 signature
    doc["workload_identity"] = {
        "spiffe_id": "spiffe://example.org/workload/tampered",
        "provider": "spiffe",
        "trust_domain": "example.org",
    }
    assert verify_attestation(doc) is False


def test_no_silent_downgrade_when_identity_required() -> None:
    priv_pem, _ = generate_keypair()
    # Legacy key-only attestation without workload identity
    legacy_attest = sign_chain(
        priv_pem,
        run_id="run_legacy",
        trusted_through_seq=8,
        chain_hash="hash_legacy",
    )
    assert legacy_attest.workload_identity is None

    # Key-only verification still passes (backward compatibility)
    assert verify_attestation(legacy_attest, expected_chain_hash="hash_legacy") is True

    # When workload identity is explicitly required, legacy attestation is rejected
    assert (
        verify_attestation(
            legacy_attest,
            require_workload_identity=True,
        )
        is False
    )
    assert (
        verify_attestation(
            legacy_attest,
            expected_workload_identity="spiffe://example.org/workload/required",
        )
        is False
    )


def test_cli_attest_and_verify_with_workload_identity(tmp_path: Path) -> None:
    from continuum.cli import ExitCode
    from continuum.events import EventType
    from continuum.models import Run
    from continuum.storage import SQLiteStorage
    from tests.test_cli import run

    db_path = str(tmp_path / "cli_test.db")
    with SQLiteStorage(db_path) as store:
        store.create_run(Run(run_id="run_wid", goal="test workload identity"))
        store.append_event(
            "run_wid", EventType.RUN_STARTED, {"goal": "test workload identity", "total": 1}
        )

    priv_pem, _ = generate_keypair()
    key_file = tmp_path / "signer.pem"
    key_file.write_text(priv_pem, encoding="utf-8")

    attest_file = tmp_path / "run_wid.attest.json"
    code, out, err = run(
        "--db",
        db_path,
        "attest",
        "run_wid",
        "--key",
        str(key_file),
        "--signer",
        "ci-bot",
        "--workload-identity",
        "spiffe://example.org/ns/prod/sa/agent-1",
        "--out",
        str(attest_file),
    )
    assert code == ExitCode.OK, err
    assert attest_file.exists()

    data = json.loads(attest_file.read_text(encoding="utf-8"))
    assert data["workload_identity"]["spiffe_id"] == "spiffe://example.org/ns/prod/sa/agent-1"

    # Verify with matching expected identity
    code, out, _ = run(
        "--db",
        db_path,
        "attest-verify",
        "run_wid",
        "--attest",
        str(attest_file),
        "--expected-workload-identity",
        "spiffe://example.org/ns/prod/sa/agent-1",
    )
    assert code == ExitCode.OK
    assert "SIGNED" in out
    assert "spiffe://example.org/ns/prod/sa/agent-1" in out

    # Verify with mismatching expected identity
    code, out, _ = run(
        "--db",
        db_path,
        "attest-verify",
        "run_wid",
        "--attest",
        str(attest_file),
        "--expected-workload-identity",
        "spiffe://example.org/ns/prod/sa/agent-other",
    )
    assert code == ExitCode.CORRUPTED
    assert "IDENTITY_MISMATCH" in out

    # Resume with matching attestation and expected identity
    code, out, _ = run(
        "--db",
        db_path,
        "resume",
        "run_wid",
        "--attest",
        str(attest_file),
        "--require-workload-identity",
        "spiffe://example.org/ns/prod/sa/agent-1",
    )
    assert code == ExitCode.OK

    # Resume with mismatching required identity fails safely
    code, out, _ = run(
        "--db",
        db_path,
        "resume",
        "run_wid",
        "--attest",
        str(attest_file),
        "--require-workload-identity",
        "spiffe://example.org/ns/prod/sa/agent-other",
    )
    assert code == ExitCode.UNSAFE
    assert "Attestation verification failed" in out
