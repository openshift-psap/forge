"""
MLflow Tracing Performance Testing Toolbox

Project-specific commands for MLflow platform management and test infrastructure
on OpenShift AI (RHOAI) clusters.

Assumes RHOAI is already installed but not patched for MLflow.

Modules:
    install_platform/           - Enable MLflow operator + create perf-testing namespace
    cleanup_platform/           - Disable MLflow operator + delete namespace
    apply_infrastructure/       - Deploy MinIO, PostgreSQL, MLflow CR, secrets, RBAC;
                                  once per test-matrix run (PVC sized for the
                                  largest pg_size tier)
    seed_postgres/             - Grow PostgreSQL with cloned MLflow traces/spans
                                  to a target database size
    cleanup_test_resources/     - Remove all per-run resources (reverse of apply_infrastructure)
"""
