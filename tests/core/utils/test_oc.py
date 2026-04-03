"""Unit tests for OC wrapper with retry logic."""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from projects.core.utils import OC, OCResult, RetryConfig


class TestRetryConfig:
    """Tests for RetryConfig dataclass."""

    def test_default_values(self):
        """RetryConfig has sensible defaults."""
        config = RetryConfig()

        assert config.max_retries == 3
        assert config.initial_delay == 1.0
        assert config.max_delay == 30.0
        assert config.backoff_multiplier == 2.0
        assert config.retry_on_timeout is True

    def test_custom_values(self):
        """RetryConfig accepts custom values."""
        config = RetryConfig(
            max_retries=5,
            initial_delay=0.5,
            max_delay=60.0,
            backoff_multiplier=3.0,
            retry_on_timeout=False,
        )

        assert config.max_retries == 5
        assert config.initial_delay == 0.5
        assert config.max_delay == 60.0
        assert config.backoff_multiplier == 3.0
        assert config.retry_on_timeout is False


class TestOCResult:
    """Tests for OCResult dataclass."""

    def test_from_completed_process_success(self):
        """OCResult created from successful subprocess."""
        mock_result = MagicMock(spec=subprocess.CompletedProcess)
        mock_result.returncode = 0
        mock_result.stdout = "pod/my-pod created"
        mock_result.stderr = ""

        result = OCResult.from_completed_process(
            mock_result,
            command=["oc", "apply", "-f", "test.yaml"],
            attempts=1,
            duration=0.5,
        )

        assert result.success is True
        assert result.returncode == 0
        assert result.stdout == "pod/my-pod created"
        assert result.stderr == ""
        assert result.attempts == 1
        assert result.duration == 0.5

    def test_from_completed_process_failure(self):
        """OCResult created from failed subprocess."""
        mock_result = MagicMock(spec=subprocess.CompletedProcess)
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: resource not found"

        result = OCResult.from_completed_process(
            mock_result,
            command=["oc", "get", "pod", "missing"],
            attempts=3,
            duration=5.0,
        )

        assert result.success is False
        assert result.returncode == 1
        assert result.stderr == "error: resource not found"
        assert result.attempts == 3

    def test_from_error(self):
        """OCResult created from exception."""
        error = subprocess.TimeoutExpired(cmd=["oc", "get", "pods"], timeout=30)

        result = OCResult.from_error(
            error,
            command=["oc", "get", "pods"],
            attempts=4,
            duration=120.0,
        )

        assert result.success is False
        assert result.returncode == -1
        assert "timed out" in result.stderr.lower() or "timeout" in result.stderr.lower()
        assert result.attempts == 4


class TestOC:
    """Tests for OC wrapper class."""

    def test_init_defaults(self):
        """OC initializes with sensible defaults."""
        oc = OC()

        assert oc.namespace is None
        assert oc.timeout == 60
        assert isinstance(oc.retry, RetryConfig)

    def test_init_with_namespace(self):
        """OC accepts namespace."""
        oc = OC(namespace="forge")

        assert oc.namespace == "forge"

    def test_init_with_custom_retry(self):
        """OC accepts custom retry config."""
        config = RetryConfig(max_retries=10)
        oc = OC(retry=config)

        assert oc.retry.max_retries == 10

    def test_build_cmd_with_namespace(self):
        """Commands include namespace when set."""
        oc = OC(namespace="forge")
        cmd = oc._build_cmd(["get", "pods"])

        assert cmd == ["oc", "-n", "forge", "get", "pods"]

    def test_build_cmd_without_namespace(self):
        """Commands work without namespace."""
        oc = OC()
        cmd = oc._build_cmd(["get", "namespaces"])

        assert cmd == ["oc", "get", "namespaces"]

    def test_build_cmd_namespace_override(self):
        """Namespace can be overridden per command."""
        oc = OC(namespace="default")
        cmd = oc._build_cmd(["get", "pods"], namespace="other")

        assert cmd == ["oc", "-n", "other", "get", "pods"]

    @patch("subprocess.run")
    def test_get_success(self, mock_run):
        """get() returns successful result."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="NAME   READY   STATUS\nmy-pod   1/1   Running",
            stderr="",
        )

        oc = OC(namespace="forge")
        result = oc.get("pods")

        assert result.success is True
        assert "my-pod" in result.stdout
        mock_run.assert_called_once()
        call_args = mock_run.call_args
        assert "oc" in call_args[0][0]
        assert "-n" in call_args[0][0]
        assert "forge" in call_args[0][0]
        assert "get" in call_args[0][0]
        assert "pods" in call_args[0][0]

    @patch("subprocess.run")
    def test_get_with_selector(self, mock_run):
        """get() passes additional arguments."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        oc = OC(namespace="forge")
        oc.get("pods", "-l", "app=vllm", "-o", "yaml")

        call_args = mock_run.call_args[0][0]
        assert "-l" in call_args
        assert "app=vllm" in call_args
        assert "-o" in call_args
        assert "yaml" in call_args

    @patch("subprocess.run")
    def test_apply_success(self, mock_run):
        """apply() works with file path."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="pod/my-pod created",
            stderr="",
        )

        oc = OC(namespace="forge")
        result = oc.apply("-f", "manifest.yaml")

        assert result.success is True
        call_args = mock_run.call_args[0][0]
        assert "apply" in call_args
        assert "-f" in call_args
        assert "manifest.yaml" in call_args

    @patch("subprocess.run")
    def test_apply_with_stdin(self, mock_run):
        """apply() accepts input via stdin."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        oc = OC(namespace="forge")
        yaml_content = "apiVersion: v1\nkind: Pod\n..."
        oc.apply("-f", "-", input=yaml_content)

        call_args = mock_run.call_args
        assert call_args.kwargs.get("input") == yaml_content

    @patch("subprocess.run")
    def test_delete_success(self, mock_run):
        """delete() deletes resources."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='pod "my-pod" deleted',
            stderr="",
        )

        oc = OC(namespace="forge")
        result = oc.delete("pod", "my-pod")

        assert result.success is True
        call_args = mock_run.call_args[0][0]
        assert "delete" in call_args
        assert "pod" in call_args
        assert "my-pod" in call_args

    @patch("subprocess.run")
    def test_delete_with_ignore_not_found(self, mock_run):
        """delete() passes extra flags."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        oc = OC(namespace="forge")
        oc.delete("pod", "my-pod", "--ignore-not-found")

        call_args = mock_run.call_args[0][0]
        assert "--ignore-not-found" in call_args

    @patch("subprocess.run")
    def test_logs_success(self, mock_run):
        """logs() retrieves pod logs."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout="INFO: Server started\nINFO: Ready",
            stderr="",
        )

        oc = OC(namespace="forge")
        result = oc.logs("my-pod")

        assert result.success is True
        assert "Server started" in result.stdout
        call_args = mock_run.call_args[0][0]
        assert "logs" in call_args
        assert "my-pod" in call_args

    @patch("subprocess.run")
    def test_logs_with_container(self, mock_run):
        """logs() accepts container flag."""
        mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")

        oc = OC(namespace="forge")
        oc.logs("my-pod", "-c", "sidecar", "--tail=100")

        call_args = mock_run.call_args[0][0]
        assert "-c" in call_args
        assert "sidecar" in call_args
        assert "--tail=100" in call_args

    @patch("subprocess.run")
    def test_exec_success(self, mock_run):
        """exec() runs command in pod."""
        mock_run.return_value = MagicMock(
            returncode=0,
            stdout='{"status": "healthy"}',
            stderr="",
        )

        oc = OC(namespace="forge")
        result = oc.exec("my-pod", "--", "curl", "localhost:8080/health")

        assert result.success is True
        call_args = mock_run.call_args[0][0]
        assert "exec" in call_args
        assert "my-pod" in call_args
        assert "--" in call_args
        assert "curl" in call_args


class TestOCRetry:
    """Tests for OC retry behavior."""

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_on_transient_error(self, mock_run, mock_sleep):
        """OC retries on transient network errors."""
        # First call fails with connection error, second succeeds
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr="connection refused"),
            MagicMock(returncode=0, stdout="success", stderr=""),
        ]

        oc = OC(namespace="forge", retry=RetryConfig(max_retries=3, initial_delay=0.1))
        result = oc.get("pods")

        assert result.success is True
        assert result.attempts == 2
        assert mock_run.call_count == 2
        mock_sleep.assert_called_once()  # Slept once between retries

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_on_permanent_error(self, mock_run, mock_sleep):
        """OC does not retry on non-transient errors."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="error: resource not found",
        )

        oc = OC(namespace="forge", retry=RetryConfig(max_retries=3))
        result = oc.get("pod", "nonexistent")

        assert result.success is False
        assert result.attempts == 1
        assert mock_run.call_count == 1
        mock_sleep.assert_not_called()  # No sleep - didn't retry

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_exhausted(self, mock_run, mock_sleep):
        """OC returns failure after exhausting retries."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="connection timed out",
        )

        oc = OC(namespace="forge", retry=RetryConfig(max_retries=2, initial_delay=0.1))
        result = oc.get("pods")

        assert result.success is False
        assert result.attempts == 3  # Initial + 2 retries
        assert mock_run.call_count == 3
        assert mock_sleep.call_count == 2  # Slept between each retry

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_retry_on_timeout(self, mock_run, mock_sleep):
        """OC retries on subprocess timeout."""
        mock_run.side_effect = [
            subprocess.TimeoutExpired(cmd=["oc"], timeout=30),
            MagicMock(returncode=0, stdout="success", stderr=""),
        ]

        oc = OC(
            namespace="forge",
            retry=RetryConfig(max_retries=3, retry_on_timeout=True, initial_delay=0.1),
        )
        result = oc.get("pods")

        assert result.success is True
        assert result.attempts == 2

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_no_retry_on_timeout_when_disabled(self, mock_run, mock_sleep):
        """OC does not retry timeout when disabled."""
        mock_run.side_effect = subprocess.TimeoutExpired(cmd=["oc"], timeout=30)

        oc = OC(
            namespace="forge",
            retry=RetryConfig(max_retries=3, retry_on_timeout=False),
        )
        result = oc.get("pods")

        assert result.success is False
        assert result.attempts == 1
        mock_sleep.assert_not_called()

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_exponential_backoff(self, mock_run, mock_sleep):
        """OC uses exponential backoff between retries."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="service unavailable",
        )

        oc = OC(
            namespace="forge",
            retry=RetryConfig(
                max_retries=3,
                initial_delay=1.0,
                backoff_multiplier=2.0,
                max_delay=10.0,
            ),
        )
        oc.get("pods")

        # Check backoff delays: 1.0, 2.0, 4.0
        calls = mock_sleep.call_args_list
        assert len(calls) == 3
        assert calls[0][0][0] == 1.0
        assert calls[1][0][0] == 2.0
        assert calls[2][0][0] == 4.0

    @patch("time.sleep")
    @patch("subprocess.run")
    def test_max_delay_cap(self, mock_run, mock_sleep):
        """OC caps delay at max_delay."""
        mock_run.return_value = MagicMock(
            returncode=1,
            stdout="",
            stderr="service unavailable",
        )

        oc = OC(
            namespace="forge",
            retry=RetryConfig(
                max_retries=5,
                initial_delay=10.0,
                backoff_multiplier=2.0,
                max_delay=15.0,
            ),
        )
        oc.get("pods")

        # Delays should be: 10.0, 15.0 (capped), 15.0, 15.0, 15.0
        calls = mock_sleep.call_args_list
        assert calls[0][0][0] == 10.0
        assert calls[1][0][0] == 15.0  # Capped
        assert calls[2][0][0] == 15.0
        assert calls[3][0][0] == 15.0
        assert calls[4][0][0] == 15.0


class TestOCTransientErrorDetection:
    """Tests for transient error detection."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "connection refused",
            "Connection reset by peer",
            "Unable to connect to the server",
            "no route to host",
            "etcdserver: request timed out",
            "context deadline exceeded",
            "the server was unable to return a response",
            "unexpected EOF",
            "i/o timeout",
            "TLS handshake timeout",
            "Service Unavailable",
            "too many requests",
        ],
    )
    @patch("time.sleep")
    @patch("subprocess.run")
    def test_transient_error_patterns(self, mock_run, mock_sleep, stderr):
        """OC recognizes various transient error patterns."""
        mock_run.side_effect = [
            MagicMock(returncode=1, stdout="", stderr=stderr),
            MagicMock(returncode=0, stdout="success", stderr=""),
        ]

        oc = OC(retry=RetryConfig(max_retries=1, initial_delay=0.1))
        result = oc.get("pods")

        assert result.success is True
        assert mock_run.call_count == 2, f"Should retry for: {stderr}"

    @pytest.mark.parametrize(
        "stderr",
        [
            "error: resource not found",
            "Error: pod not found",
            "forbidden: User cannot get resource",
            "invalid: spec.containers: Required value",
        ],
    )
    @patch("time.sleep")
    @patch("subprocess.run")
    def test_non_transient_error_patterns(self, mock_run, mock_sleep, stderr):
        """OC does not retry non-transient errors."""
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr=stderr)

        oc = OC(retry=RetryConfig(max_retries=3))
        result = oc.get("pods")

        assert result.success is False
        assert mock_run.call_count == 1, f"Should not retry for: {stderr}"
        mock_sleep.assert_not_called()
