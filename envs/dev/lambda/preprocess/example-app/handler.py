import json, os, datetime
from collections import defaultdict
import boto3

cw = boto3.client("cloudwatch")
NAMESPACE = os.environ.get("METRIC_NAMESPACE", "Observability/ExampleApp")

STATUS_MAP = {
    "serviceoperational": ("OK", 0),
    "servicerestored": ("OK", 0),
    "resolved": ("OK", 0),
    "resolvedexternal": ("OK", 0),
    "falsepositive": ("OK", 0),
    "postincidentreviewpublished": ("OK", 0),
    "investigating": ("INVESTIGATING", 2),
    "confirmed": ("INVESTIGATING", 2),
    "reported": ("INVESTIGATING", 1),
    "investigationsuspended": ("INVESTIGATING", 1),
    "restoringservice": ("RECOVERING", 1),
    "extendedrecovery": ("RECOVERING", 1),
    "verifyingservice": ("RECOVERING", 1),
    "mitigated": ("RECOVERING", 1),
    "mitigatedexternal": ("RECOVERING", 1),
    "servicedegradation": ("DEGRADED", 2),
    "serviceinterruption": ("OUTAGE", 3),
    "unknownfuturevalue": ("UNKNOWN", 1),
}

SEVERITY_ORDER = {
    "OK": 0,
    "RECOVERING": 1,
    "INVESTIGATING": 2,
    "DEGRADED": 2,
    "OUTAGE": 3,
    "UNKNOWN": 1,
}

ISSUE_SEV_SCORE = {
    "informational": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 3,
}


def _now_iso():
    return datetime.datetime.utcnow().replace(microsecond=0).isoformat() + "Z"


def _norm_status(raw):
    return STATUS_MAP.get((raw or "").replace(" ", "").lower(), ("UNKNOWN", 1))


def _extract_arrays(body):
    # Supports both {value:[...]} and {healthOverviews:[...], issues:[...]}
    if isinstance(body, dict) and (
        "healthOverviews" in body or "issues" in body
    ):
        return body.get("healthOverviews") or [], body.get("issues") or []
    if isinstance(body, dict) and isinstance(body.get("value"), list):
        return body["value"], []
    return [], []


def _is_issue_open(it: dict) -> bool:
    status = (it.get("status") or "").replace(" ", "").lower()
    return status not in {"servicerestored", "resolved", "closed"}


def lambda_handler(event, context):
    app = event.get("appName") or "unknown-app"
    poll = event.get("poll") or {}
    if not (poll.get("ok") and poll.get("status") == 200):
        return {
            "ok": False,
            "error": "poll not ok",
            "status": poll.get("status"),
        }

    try:
        body = json.loads(poll.get("body") or "{}")
    except Exception as e:
        return {"ok": False, "error": f"invalid json: {e}"}

    health_overviews, issues = _extract_arrays(body)

    issues_by_service = defaultdict(list)
    for it in issues:
        svc = it.get("service") or it.get("affectedWorkload")
        if svc:
            issues_by_service[svc].append(it)

    services = []
    for ho in health_overviews:
        name = ho.get("service") or ho.get("id") or "unknown"
        raw = ho.get("status")
        cat, sev_code = _norm_status(raw)
        open_issues = [
            i for i in issues_by_service.get(name, []) if _is_issue_open(i)
        ]
        highest_issue_sev = max(
            (
                ISSUE_SEV_SCORE.get((i.get("severity") or "").lower(), 1)
                for i in open_issues
            ),
            default=0,
        )
        sev = max(sev_code, highest_issue_sev)

        services.append(
            {
                "id": ho.get("id") or name,
                "name": name,
                "rawStatus": raw,
                "statusCategory": cat,
                "severity": sev,
                "openIssues": len(open_issues),
                "highestIncidentSeverity": highest_issue_sev,
            }
        )

    total = len(services) or 1
    ok = sum(1 for s in services if s["statusCategory"] == "OK")
    degraded = sum(1 for s in services if s["statusCategory"] == "DEGRADED")
    outage = sum(1 for s in services if s["statusCategory"] == "OUTAGE")
    recovering = sum(
        1 for s in services if s["statusCategory"] == "RECOVERING"
    )
    investigating = sum(
        1 for s in services if s["statusCategory"].startswith("INVESTIGATING")
    )
    availability = round(100.0 * ok / total, 2)
    critical = max((s["severity"] for s in services), default=0)

    # Emit CloudWatch metrics (best-effort)
    try:
        cw.put_metric_data(
            Namespace=NAMESPACE,
            MetricData=[
                {
                    "MetricName": "OverallAvailabilityPercent",
                    "Value": availability,
                    "Unit": "Percent",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
                {
                    "MetricName": "ServicesOutageCount",
                    "Value": outage,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
                {
                    "MetricName": "ServicesDegradedCount",
                    "Value": degraded,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
                {
                    "MetricName": "ServicesRecoveringCount",
                    "Value": recovering,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
                {
                    "MetricName": "ServicesInvestigatingCount",
                    "Value": investigating,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
                {
                    "MetricName": "CriticalScore",
                    "Value": critical,
                    "Unit": "Count",
                    "Dimensions": [{"Name": "AppName", "Value": app}],
                },
            ],
        )
    except Exception:
        pass

    record = {
        "appName": app,
        "observedAt": _now_iso(),
        "source": {
            "provider": "microsoft-graph",
            "dataset": "serviceAnnouncement",
            "httpStatus": poll.get("status"),
        },
        "overall": {
            "statusCategory": (
                "OUTAGE"
                if outage
                else (
                    "DEGRADED"
                    if degraded
                    else (
                        "RECOVERING"
                        if recovering
                        else "OK" if ok == total else "UNKNOWN"
                    )
                )
            ),
            "availabilityPercent": availability,
            "impactedServicesCount": degraded
            + outage
            + recovering
            + investigating,
            "degradedCount": degraded,
            "outageCount": outage,
            "recoveringCount": recovering,
            "investigatingCount": investigating,
            "criticalScore": critical,
        },
        "services": services,
        "counts": {"totalServices": len(services)},
        "version": "1.0",
    }

    return {"ok": True, "raw": record}
