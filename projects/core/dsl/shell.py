import logging
import shlex
import subprocess
from dataclasses import dataclass
from pathlib import Path

import projects.core.library.env as env
from projects.core.library.run import SignalInterrupt

logger = logging.getLogger("DSL")
logger.propagate = False  # Don't show logger prefix


@dataclass
class CommandResult:
    """Result of a command execution"""

    stdout: str
    stderr: str
    returncode: int
    command: str

    @property
    def success(self) -> bool:
        return self.returncode == 0


def run(
    command: str | list[str],
    check: bool = True,
    shell: bool = True,
    stdout_dest: str | Path | None = None,
    log_stdout: bool = True,
    log_stderr: bool = True,
    input_text: str | None = None,
    timeout_seconds: float | None = None,
    text: bool = True,
    handled_secretly: bool = False,
) -> CommandResult:
    """
    Execute a shell command

    Args:
        command: Command to execute (string for shell=True, list for shell=False)
        check: Raise exception on non-zero exit code
        shell: Execute through shell
        stdout_dest: Optional file path to write stdout to
        log_stdout: Optional. If False, don't log the content of stdout.
        log_stderr: Optional. If False, don't log the content of stderr.
        input_text: Optional text to send to command's stdin
        timeout_seconds: Optional timeout in seconds
        text: Optional. If False, handle binary output (default True)
        handled_secretly: If True, suppress command logging to prevent secret leakage
    Returns:
        CommandResult with execution details
    """
    # Handle both string and list commands
    if isinstance(command, list):
        command_for_logging = " ".join(shlex.quote(str(arg)) for arg in command)
        command_for_subprocess = command
    else:
        command_for_logging = command
        command_for_subprocess = command

    # Print command in verbose format
    if handled_secretly:
        logger.info("== command == ")
        logger.info("| <command suppressed for secret handling>")
    else:
        logger.info("== command == ")
        logger.info(f"| <command> {command_for_logging}")

    try:
        result = subprocess.run(
            command_for_subprocess,
            shell=shell,
            check=False,  # We handle check ourselves
            capture_output=True,
            input=input_text,
            timeout=timeout_seconds,
            text=text,
        )

        # Handle text vs binary output
        if text:
            stdout_str = result.stdout or ""
            stderr_str = result.stderr or ""
        else:
            # For binary mode, convert to string representation for CommandResult
            stdout_str = f"<binary data: {len(result.stdout)} bytes>" if result.stdout else ""
            stderr_str = result.stderr.decode("utf-8", errors="replace") if result.stderr else ""

        cmd_result = CommandResult(
            stdout=stdout_str,
            stderr=stderr_str,
            returncode=result.returncode,
            command="<suppressed for secret handling>" if handled_secretly else command,
        )

        # Write stdout to file if requested
        if stdout_dest:
            stdout_path = Path(stdout_dest)
            stdout_path.parent.mkdir(parents=True, exist_ok=True)
            with open(stdout_path, "w" if text else "wb") as f:
                f.write(stdout_str if text else result.stdout)

        # Print output in verbose format
        if text:
            if result.stdout:
                if stdout_dest:
                    logger.info(f"| <stdout saved into {stdout_dest}>")
                elif log_stdout:
                    stdout_lines = stdout_str.strip().splitlines()
                    if len(stdout_lines) == 1:
                        logger.info(f"| <stdout> {stdout_lines[0]}")
                    else:
                        logger.info("| <stdout>\n" + "\n|   ".join(stdout_lines) + "\n| </stdout>")
                else:
                    logger.info("| <stdout logging skipped>")

            if result.stderr:
                if log_stderr:
                    stderr_lines = stderr_str.strip().splitlines()
                    if len(stderr_lines) == 1:
                        logger.info(f"| <stderr> {stderr_lines[0]}")
                    else:
                        logger.info("| <stderr>\n" + "\n|   ".join(stderr_lines) + "\n| </stderr>")
                else:
                    logger.info("| <stderr logging skipped>")

            if not (result.stdout or result.stderr):
                logger.info("| <no output>")
        else:
            # Binary mode - always show lengths
            if result.stdout:
                if stdout_dest:
                    logger.info(
                        f"| <binary stdout saved into {stdout_dest}> ({len(result.stdout)} bytes)"
                    )
                else:
                    logger.info(f"| <binary stdout> {len(result.stdout)} bytes")

            if result.stderr:
                # Try to decode stderr as text for logging (it's usually text even in binary mode)
                try:
                    stderr_text = result.stderr.decode("utf-8", errors="replace").strip()
                    if stderr_text:
                        stderr = stderr_text.splitlines()
                        if len(stderr) == 1:
                            logger.info(f"| <stderr> {stderr[0]}")
                        else:
                            logger.info("| <stderr>\n" + "\n|   ".join(stderr) + "\n| </stderr>")
                except Exception:
                    logger.info(f"| <binary stderr> {len(result.stderr)} bytes")

            if not (result.stdout or result.stderr):
                logger.info("| <no output>")

        if result.returncode != 0:
            logger.info(f"| <exit_code> {result.returncode}")

        logger.info("==")

        if check and result.returncode != 0:
            if handled_secretly:
                raise subprocess.CalledProcessError(
                    result.returncode,
                    "command failed (suppressed for secret handling)",
                    "",
                    "error output suppressed for secret handling",
                )

            # Create a more informative error message
            error_msg = f"Command failed with exit code {result.returncode}: {command}"
            if result.stderr:
                error_msg += f"\nSTDERR: {stderr_str.strip()}"
            if result.stdout:
                error_msg += f"\nSTDOUT: {stdout_str.strip()}"

            # Create exception with enhanced message
            error = subprocess.CalledProcessError(
                result.returncode, command, result.stdout, result.stderr
            )
            error.args = (error_msg,)
            raise error

        return cmd_result

    except (KeyboardInterrupt, SignalInterrupt):
        raise
    except Exception as e:
        logger.error(f"<{e.__class__.__name__}> {e}")
        logger.info("")
        raise


def mkdir(path, *, parents=True, exists_ok=True):
    """Create a directory with default arguments"""

    logger.info("== shell == ")
    logger.info(f"| <mkdir> {path}")

    if not isinstance(path, Path):
        path = Path(path)

    if not path.is_absolute():
        path = env.ARTIFACT_DIR / path

    logger.info("==")

    return path.mkdir(parents=parents, exist_ok=exists_ok)
