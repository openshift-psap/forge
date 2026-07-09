from __future__ import annotations

from projects.cluster.toolbox.cleanup_operators import main as cleanup_operators


def test_subscription_owner_refs_from_installplans() -> None:
    installplans = {
        "items": [
            {"metadata": {"ownerReferences": [{"kind": "Subscription", "name": "other-operator"}]}},
            {
                "metadata": {
                    "ownerReferences": [
                        {"kind": "Subscription", "name": "authorino-operator"},
                        {"kind": "Subscription", "name": "dns-operator"},
                        {"kind": "ClusterServiceVersion", "name": "ignored-csv"},
                        {"kind": "Subscription", "name": "authorino-operator"},
                    ]
                }
            },
        ]
    }

    assert cleanup_operators._subscription_owner_refs_from_installplans(
        installplans, "authorino-operator"
    ) == ["dns-operator"]


def test_expand_operators_with_installplan_owners(monkeypatch) -> None:
    installplans_by_namespace = {
        "redhat-ods-operator": {
            "items": [
                {
                    "metadata": {
                        "ownerReferences": [
                            {"kind": "Subscription", "name": "rhods-operator"},
                            {
                                "kind": "Subscription",
                                "name": "authorino-operator-stable-redhat-operators-openshift-marketplace",
                            },
                            {
                                "kind": "Subscription",
                                "name": "dns-operator-stable-redhat-operators-openshift-marketplace",
                            },
                            {
                                "kind": "Subscription",
                                "name": "limitador-operator-stable-redhat-operators-openshift-marketplace",
                            },
                            {"kind": "Subscription", "name": "rhcl-operator"},
                        ]
                    }
                },
                {
                    "metadata": {
                        "ownerReferences": [
                            {
                                "kind": "Subscription",
                                "name": "authorino-operator-stable-redhat-operators-openshift-marketplace",
                            },
                            {"kind": "Subscription", "name": "rhods-operator"},
                        ]
                    }
                },
            ]
        },
        "openshift-operators": {
            "items": [
                {
                    "metadata": {
                        "ownerReferences": [{"kind": "Subscription", "name": "rhcl-operator"}]
                    }
                }
            ]
        },
    }

    fetch_count = {}

    def fake_fetch(namespace: str) -> dict:
        fetch_count[namespace] = fetch_count.get(namespace, 0) + 1
        return installplans_by_namespace[namespace]

    monkeypatch.setattr(
        cleanup_operators,
        "_fetch_installplans",
        fake_fetch,
    )

    assert cleanup_operators._expand_operators_with_installplan_owners(
        [
            ("rhods-operator", "redhat-ods-operator"),
            ("rhcl-operator", "openshift-operators"),
        ]
    ) == [
        ("rhods-operator", "redhat-ods-operator"),
        ("rhcl-operator", "openshift-operators"),
        (
            "authorino-operator-stable-redhat-operators-openshift-marketplace",
            "redhat-ods-operator",
        ),
        (
            "dns-operator-stable-redhat-operators-openshift-marketplace",
            "redhat-ods-operator",
        ),
        (
            "limitador-operator-stable-redhat-operators-openshift-marketplace",
            "redhat-ods-operator",
        ),
        ("rhcl-operator", "redhat-ods-operator"),
    ]
    assert fetch_count == {"redhat-ods-operator": 1, "openshift-operators": 1}
