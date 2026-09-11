from __future__ import annotations

import re
from pathlib import Path

import pytest

yaml = pytest.importorskip("yaml")


ROOT = Path(__file__).resolve().parents[1]
ALERTS_PATH = ROOT / "infra" / "alerts" / "hosted-facilitator-alerts.yaml"
PRODUCTION_ALERTS_PATH = ROOT / "infra" / "alerts" / "hosted-facilitator-alerts.production.yaml"
RUNBOOK_PATH = ROOT / "docs" / "runbooks" / "hosted-facilitator-ops.md"


def _alerts_by_name(path: Path = ALERTS_PATH) -> dict[str, dict]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    alerts: dict[str, dict] = {}
    for group in data["groups"]:
        for rule in group["rules"]:
            alerts[rule["alert"]] = rule
    return alerts


def _runbook_anchors() -> set[str]:
    anchors: set[str] = set()
    for line in RUNBOOK_PATH.read_text(encoding="utf-8").splitlines():
        if not line.startswith("#"):
            continue
        title = line.lstrip("#").strip().lower()
        anchor = re.sub(r"[^a-z0-9 -]", "", title).replace(" ", "-")
        anchors.add(anchor)
    return anchors


def test_hosted_alert_rules_pin_required_alpha_blockers():
    alerts = _alerts_by_name()

    required = {
        "OmniClawHostedMetricsExporterDown",
        "OmniClawHostedFacilitatorDown",
        "OmniClawHostedReconcilerDown",
        "OmniClawHostedMetricsExporterScrapeFailure",
        "OmniClawHostedUnknownSettlementSpike",
        "OmniClawHostedUnknownSettlementBacklog",
        "OmniClawHostedManualReviewBacklog",
        "OmniClawHostedSubmittedSettlementBacklog",
        "OmniClawHostedSettleInProgressBacklog",
        "OmniClawHostedProviderErrors",
        "OmniClawHostedDuplicateSettleSpike",
        "OmniClawHostedExactSignerGasLow",
        "OmniClawHostedExactSignerGasReserveLow",
        "OmniClawHostedGatewayCanaryBalanceLow",
        "OmniClawHostedGatewayCanaryReserveLow",
        "OmniClawHostedOtelCollectorUnavailable",
    }

    assert required <= set(alerts)
    assert (
        "absent(omniclaw_hosted_facilitator_up)" in alerts["OmniClawHostedFacilitatorDown"]["expr"]
    )
    assert (
        "absent(omniclaw_hosted_metrics_exporter_scrape_success)"
        in alerts["OmniClawHostedMetricsExporterScrapeFailure"]["expr"]
    )
    assert 'status="submitted"' in alerts["OmniClawHostedSubmittedSettlementBacklog"]["expr"]
    assert 'status="settle_in_progress"' in alerts["OmniClawHostedSettleInProgressBacklog"]["expr"]
    assert "_total" in alerts["OmniClawHostedProviderErrors"]["expr"]
    assert "_total" in alerts["OmniClawHostedDuplicateSettleSpike"]["expr"]
    assert (
        "omniclaw_hosted_exact_signer_native_wei"
        in alerts["OmniClawHostedExactSignerGasReserveLow"]["expr"]
    )
    assert (
        "omniclaw_hosted_gateway_canary_available_atomic"
        in alerts["OmniClawHostedGatewayCanaryReserveLow"]["expr"]
    )


def test_hosted_alert_runbook_links_resolve_to_existing_sections():
    anchors = _runbook_anchors()

    for alert in _alerts_by_name().values():
        runbook = alert.get("annotations", {}).get("runbook", "")
        assert runbook.startswith("docs/runbooks/hosted-facilitator-ops.md#")
        anchor = runbook.rsplit("#", 1)[1]
        assert anchor in anchors


@pytest.mark.parametrize("path", [ALERTS_PATH, PRODUCTION_ALERTS_PATH])
def test_reserve_alerts_are_warning_only_before_hard_floor(path: Path):
    alerts = _alerts_by_name(path)

    signer_expr = alerts["OmniClawHostedExactSignerGasReserveLow"]["expr"]
    assert "omniclaw_hosted_exact_signer_min_native_wei > 0" in signer_expr
    assert (
        "omniclaw_hosted_exact_signer_native_wei / omniclaw_hosted_exact_signer_min_native_wei < 2"
        in signer_expr
    )
    assert "omniclaw_hosted_exact_signer_above_min == 1" in signer_expr
    assert alerts["OmniClawHostedExactSignerGasReserveLow"]["labels"]["severity"] == "ticket"
    assert alerts["OmniClawHostedExactSignerGasReserveLow"]["for"] == "10m"

    gateway_expr = alerts["OmniClawHostedGatewayCanaryReserveLow"]["expr"]
    assert "omniclaw_hosted_gateway_canary_min_atomic > 0" in gateway_expr
    assert (
        "omniclaw_hosted_gateway_canary_available_atomic / omniclaw_hosted_gateway_canary_min_atomic < 2"
        in gateway_expr
    )
    assert "omniclaw_hosted_gateway_canary_above_min == 1" in gateway_expr
    assert alerts["OmniClawHostedGatewayCanaryReserveLow"]["labels"]["severity"] == "ticket"
    assert alerts["OmniClawHostedGatewayCanaryReserveLow"]["for"] == "10m"
