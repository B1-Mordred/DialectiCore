from scripts.worker_readiness_smoke import worker_readiness_summary, worker_summary


def test_worker_readiness_smoke_summarizes_active_and_missing_workers() -> None:
    summary = worker_summary(
        {
            "status": "degraded",
            "heartbeat_ttl_seconds": 90,
            "counts": {
                "configured_roles": 3,
                "active_workers": 1,
                "active_roles": 1,
                "stale_workers": 1,
                "failed_workers": 0,
                "degraded_workers": 0,
                "malformed_heartbeats": 0,
                "malformed_leases": 0,
            },
            "workers": [
                {
                    "role": "workflow-worker",
                    "worker_id": "host:1",
                    "status": "running",
                    "heartbeat_age_seconds": 3.5,
                    "stale": False,
                },
                {
                    "role": "render-worker",
                    "worker_id": "host:2",
                    "status": "running",
                    "heartbeat_age_seconds": 120.0,
                    "stale": True,
                },
            ],
        }
    )

    assert summary == {
        "status": "degraded",
        "configured_roles": 3,
        "active_workers": 1,
        "active_roles": 1,
        "missing_role_count": 2,
        "stale_workers": 1,
        "failed_workers": 0,
        "degraded_workers": 0,
        "malformed_heartbeats": 0,
        "malformed_leases": 0,
        "heartbeat_ttl_seconds": 90,
        "active_worker_ids": [
            {
                "role": "workflow-worker",
                "worker_id": "host:1",
                "status": "running",
                "heartbeat_age_seconds": 3.5,
            }
        ],
    }


def test_worker_readiness_smoke_extracts_live_readiness_worker_registry() -> None:
    assert worker_readiness_summary(
        {
            "checks": [
                {
                    "category": "worker_registry",
                    "status": "warning",
                    "warnings": ["no active worker heartbeats are present"],
                    "blockers": [],
                    "details": {
                        "failed_readiness_checks": ["active_worker_heartbeats_present"],
                        "missing_role_names": ["workflow-worker"],
                        "reason": "no active worker heartbeats are present",
                    },
                }
            ]
        }
    ) == {
        "status": "warning",
        "warnings": ["no active worker heartbeats are present"],
        "blockers": [],
        "failed_readiness_checks": ["active_worker_heartbeats_present"],
        "missing_role_names": ["workflow-worker"],
        "reason": "no active worker heartbeats are present",
    }
