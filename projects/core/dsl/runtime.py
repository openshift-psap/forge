"""
Runtime execution engine for the DSL framework
"""

import inspect
import logging
import os
import threading
import time
import types
from datetime import datetime
from pathlib import Path

import yaml

import projects.core.library.env as env
from projects.core.library.run import SignalInterrupt

from .context import create_task_parameters
from .control_flow import EarlyReturn
from .log import (
    _get_forge_relative_path,
    _get_toolbox_function_name,
    log_completion_banner,
    log_execution_banner,
    log_task_header,
    logger,
)
from .script_manager import get_script_manager

# Import from task.py to avoid circular imports
from .task import ConditionError, RetryFailure


class TaskExecutionError(Exception):
    """Custom exception that wraps task execution failures with context"""

    def __init__(
        self,
        task_name: str,
        task_description: str,
        original_exception: Exception,
        task_args: dict = None,
        task_location: str = None,
        artifact_dir: str = None,
        task_context: dict = None,
    ):
        self.task_name = task_name
        self.task_description = task_description
        self.original_exception = original_exception
        self.task_args = task_args
        self.task_location = task_location
        self.task_context = task_context
        self.artifact_dir = artifact_dir
        tb = original_exception.__traceback__

        # Skip the first 2 frames of the stack
        if tb and tb.tb_next:
            original_exception.__traceback__ = tb.tb_next.tb_next

        # Create a comprehensive error message with clear task context
        message_parts = [
            f"❌ TASK FAILURE: {task_name}: {task_description or 'No description'}",
            f"   {task_location or 'Unknown'}",
            f"   {original_exception.__class__.__name__}: {original_exception}",
        ]

        message = "\n".join(message_parts)
        super().__init__(message)

        # Preserve the original exception chain to maintain full stack trace
        # This allows the full traceback to be shown in logs


class EarlyReturnException(Exception):
    """Exception raised when a task returns an EarlyReturn, signaling early successful termination"""

    def __init__(self, early_return: EarlyReturn):
        self.early_return = early_return
        super().__init__(early_return.message)


def execute_tasks(function_args: dict = None):
    """
    Execute all registered tasks in order, respecting conditions

    Args:
        function_args: Dictionary of function arguments (from locals())
    """
    # Track the start time for elapsed time calculations
    start_time = time.time()

    # Get the command name from the caller file path for artifact directory naming
    frame = inspect.currentframe()
    caller_frame = frame.f_back
    filename = caller_frame.f_code.co_filename
    command_name = _get_toolbox_function_name(filename)

    # Get DSL runtime parameters from function args or wrapper attributes
    prefix = function_args.pop("artifact_dirname_prefix", None)
    suffix = function_args.pop("artifact_dirname_suffix", None)

    # Also check if they're stored in the calling function (from @entrypoint decorator)
    try:
        # Get the calling function from the frame
        calling_func = caller_frame.f_globals.get(caller_frame.f_code.co_name)
        if calling_func and hasattr(calling_func, "_dsl_runtime_params"):
            runtime_params = calling_func._dsl_runtime_params
            prefix = prefix or runtime_params.get("artifact_dirname_prefix")
            suffix = suffix or runtime_params.get("artifact_dirname_suffix")
    except (AttributeError, KeyError):
        # If we can't get the calling function, that's fine - just continue
        pass

    # Debug logging to see if parameters are found
    if suffix or prefix:
        logger.info(f"DSL runtime: found prefix='{prefix}', suffix='{suffix}'")

    # Prepend prefix to command name if provided
    if prefix:
        command_name = f"{prefix}_{command_name}"

    # Append suffix to command name if provided
    if suffix:
        command_name = f"{command_name}_{suffix}"

    # Log the final command name for debugging
    logger.info(f"DSL runtime: using command_name='{command_name}'")

    # Get relative filename to match task registration
    try:
        rel_filename = str(Path(filename).relative_to(env.FORGE_HOME))
    except ValueError:
        rel_filename = filename

    # Use NextArtifactDir for proper storage management
    with env.NextArtifactDir(command_name):
        # Convert function arguments to namespace object and add artifact_dir
        args = types.SimpleNamespace(**(function_args or {}))
        args.artifact_dir = env.ARTIFACT_DIR

        # Create a shared context that persists across all tasks
        shared_context = types.SimpleNamespace()

        # Store artifact dirname suffix for task logging
        shared_context._artifact_dirname_suffix = suffix

        # Create _meta directory for metadata files
        meta_dir = env.ARTIFACT_DIR / "_meta"
        meta_dir.mkdir(exist_ok=True)

        # Setup file logging first so all output is captured
        log_file, file_handler = _setup_execution_logging(env.ARTIFACT_DIR)

        # Track if @always tasks were executed after failure for log splitting
        always_executed = False

        try:
            # Log execution banner (now captured in file)
            log_execution_banner(function_args, log_file)

            # Generate metadata files
            _generate_execution_metadata(function_args, caller_frame, meta_dir)
            _generate_restart_script(function_args, caller_frame, meta_dir)
            _generate_env_file(meta_dir)

            # Execute tasks only from the calling file
            script_manager = get_script_manager()

            # Start thread-local execution context for this execution
            script_manager.start_execution_context(rel_filename)

            file_tasks = list(script_manager.get_tasks_from_file(rel_filename))

            if not file_tasks:
                logger.error(f"No tasks found for file: {rel_filename}")
                log_completion_banner(function_args, status="NO_TASKS")
                raise RuntimeError(f"No tasks found for file: {rel_filename}")

            execution_error = None
            always_task_exceptions = []
            task_index = 0
            script_stopped = False

            try:
                while task_index < len(file_tasks):
                    current_task_info = file_tasks[task_index]
                    _execute_single_task(current_task_info, args, shared_context, start_time)
                    task_index += 1

            except (KeyboardInterrupt, SignalInterrupt):
                logger.info("")
                logger.fatal("==> INTERRUPTED: Received KeyboardInterrupt (Ctrl+C)")
                logger.info("==> Exiting...")
                # Show completion banner with interrupted status
                log_completion_banner(function_args, status="INTERRUPTED")
                raise

            except EarlyReturnException as e:
                # EarlyReturn was returned - skip remaining non-@always tasks but continue with @always tasks
                logger.info("")
                logger.info("~" * 80)
                logger.info(f"==> EARLY RETURN: {e.early_return.message}")
                logger.info("~" * 80)
                # execution_error remains None so we don't treat this as a failure
                # Set flag to distinguish from error cases in pending task handling
                script_stopped = True

            except (TaskExecutionError, ConditionError, RetryFailure, Exception) as e:
                # Generate FAILURE file for agent analysis
                _generate_failure_file_for_agent(e)

                # Log error but continue to always tasks
                logger.info("")
                logger.fatal(f"==> {e.__class__.__name__}: {e}")
                log_completion_banner(function_args, status=f"EXCEPTION ({e.__class__.__name__})")

                # Save error to re-raise after always tasks execute
                execution_error = e

            # After a failure or stop, skip pending non-@always tasks; still run pending @always tasks
            pending = file_tasks[task_index + 1 :] if task_index < len(file_tasks) else []
            always_pending = [t for t in pending if t.get("always_execute")]

            # Mark the start of @always task execution for post-mortem log splitting
            if always_pending:
                if script_stopped:
                    logger.info("Executing the @always tasks after early return ...")
                else:
                    logger.warning("Executing the @always tasks ...")
                    # Only mark for log splitting if there was an execution error
                    if execution_error:
                        logger.info("=== @ALWAYS TASK EXECUTION BEGIN ===")
                        always_executed = True
            for current_task_info in pending:
                if not current_task_info.get("always_execute"):
                    logger.info("")
                    logger.info("~" * 80)
                    if script_stopped:
                        logger.info(
                            f"==> SKIPPING TASK: {current_task_info['name']} "
                            "(not @always; early return triggered)"
                        )
                    else:
                        logger.info(
                            f"==> SKIPPING TASK: {current_task_info['name']} "
                            "(not @always; aborted after earlier failure)"
                        )
                    logger.info("~" * 80)
                    continue
                try:
                    _execute_single_task(current_task_info, args, shared_context, start_time)

                except EarlyReturnException:
                    # EarlyReturn from @always task - just log and continue with other @always tasks
                    logger.info(
                        f"==> @always task {current_task_info['name']} returned EarlyReturn - continuing with other @always tasks"
                    )

                except Exception as always_exc:
                    # Collect all always task exceptions
                    always_task_exceptions.append(always_exc)
                    logger.error(f"==> ALWAYS TASK ALSO FAILED: {always_exc}")
                    logger.info("")

            # Mark end of @always execution for post-processing
            if always_executed:
                logger.info("=== @ALWAYS TASK EXECUTION END ===")

            # Re-raise accumulated errors
            all_exceptions = []
            if execution_error:
                all_exceptions.append(execution_error)
            if always_task_exceptions:
                all_exceptions.extend(always_task_exceptions)

            if all_exceptions:
                log_completion_banner(
                    function_args,
                    status=f"FAILED ({len(all_exceptions)} exception(s))",
                )
                if len(all_exceptions) == 1:
                    raise all_exceptions[0]
                else:
                    raise ExceptionGroup(
                        "Task execution failed with multiple exceptions", all_exceptions
                    )

            # Log completion banner if execution was successful
            log_completion_banner(function_args)

            # for the time being, return the shared context.  In the
            # future we'll return more info about what has been
            # executed
            shared_context.__dict__["artifact_dir"] = args.artifact_dir

            # Generate context file on successful completion
            _generate_context_file(shared_context, meta_dir)

            return shared_context

        finally:
            # Clear thread-local execution context
            script_manager.clear_execution_context()
            # Clean up the thread-local file handler to prevent leaks
            if hasattr(_thread_local_handlers, "file_handler"):
                _thread_local_handlers.file_handler.close()
                # Remove the reference to prevent memory leaks
                del _thread_local_handlers.file_handler

            # Split @always task logs to separate file after handler is closed
            if always_executed:
                _split_always_task_logs(env.ARTIFACT_DIR)


def _execute_single_task(task_info, args, shared_context, start_time=None):
    """Execute a single task with condition checking"""
    task_name = task_info["name"]
    task_func = task_info["func"]
    condition = task_info["condition"]
    task_status = task_info["status"] = {}

    # Log task header with suffix from shared context
    suffix = getattr(shared_context, "_artifact_dirname_suffix", None)

    # Get definition location from the original function
    original_func = task_func.original_func
    co_filename = original_func.__code__.co_filename
    co_firstlineno = original_func.__code__.co_firstlineno

    # Make filename relative to FORGE_HOME
    try:
        rel_definition_filename = str(Path(co_filename).relative_to(env.FORGE_HOME))
    except ValueError:
        rel_definition_filename = co_filename

    log_task_header(
        task_name,
        original_func.__doc__,
        rel_definition_filename,
        co_firstlineno,
        suffix,
        start_time,
    )

    # Check condition if present
    if condition is not None:
        try:
            # Condition should be a callable (lambda) for lazy evaluation
            if callable(condition):
                should_run = condition()
            else:
                should_run = bool(condition)

            if not should_run:
                logger.info("")
                logger.info("~" * 80)
                logger.info(f"==> SKIPPING TASK: {task_name} (condition not met)")
                logger.info("~" * 80)
                return
        except Exception as e:
            logger.error(
                f"==> CONDITION EXCEPTION raised by {task_name}: {e.__class__.__name__}: {e}"
            )
            logger.error(f"==> Task: {task_name} ({task_func.__doc__ or 'No description'})")
            logger.info("")
            raise ConditionError(e) from e

    # Execute the task
    try:
        # Create readonly args and mutable context
        readonly_args, context = create_task_parameters(args, shared_context)

        # Check if task has retry configuration
        retry_config = task_info.get("retry_config")
        if retry_config:
            # Import here to avoid circular imports
            from .task import _execute_with_retry

            task_status["ret"] = _execute_with_retry(
                task_func,
                retry_config["attempts"],
                retry_config["delay"],
                retry_config["backoff"],
                retry_config.get("retry_on_exceptions", False),
                readonly_args,
                context,
                artifact_dirname_suffix=getattr(shared_context, "_artifact_dirname_suffix", None),
            )
        else:
            # Call task with readonly args and mutable context
            task_status["ret"] = task_func(readonly_args, context)

        if task_status["ret"] is not None:
            logger.info(f"<task returned value> {task_status['ret']}")

            # Check if task returned EarlyReturn - if so, raise exception to stop execution
            if isinstance(task_status["ret"], EarlyReturn):
                logger.info(f"==> EARLY RETURN: {task_status['ret']}")
                raise EarlyReturnException(task_status["ret"])

        # Store context values back into shared_context for access by subsequent tasks
        # This allows tasks to communicate through context without polluting args
        for attr_name, attr_value in vars(context).items():
            if not attr_name.startswith("_"):
                setattr(shared_context, attr_name, attr_value)

    except (KeyboardInterrupt, SignalInterrupt):
        raise
    except EarlyReturnException:
        # Allow EarlyReturn to bubble up without wrapping in TaskExecutionError
        raise
    except Exception as e:
        co_filename = task_func.original_func.__code__.co_filename
        try:
            co_filename = Path(co_filename).relative_to(env.FORGE_HOME)
        except ValueError as path_err:
            logger.warning(
                f"Path {co_filename} isn't relative to FORGE_HOME={env.FORGE_HOME} ({path_err})"
            )
            pass  # Use absolute path if file is outside FORGE_HOME

        task_location = f"{co_filename}:{task_func.original_func.__code__.co_firstlineno}"

        # Call failure handler if one is configured
        failure_handler = task_info.get("on_failure_handler")
        if failure_handler:
            try:
                readonly_args, context = create_task_parameters(args, shared_context)
                failure_handler(readonly_args, context, e)
            except Exception as handler_error:
                logger.warning(f"Failure handler for task {task_name} failed: {handler_error}")

        # Wrap in custom exception with context
        task_error = TaskExecutionError(
            task_name=task_name,
            task_description=task_func.__doc__ or "No description",
            original_exception=e,
            task_args=vars(args) if hasattr(args, "__dict__") else None,
            task_context=vars(context) if hasattr(context, "__dict__") else None,
            task_location=task_location,
            artifact_dir=str(env.ARTIFACT_DIR),
        )
        raise task_error from e


def clear_tasks(file_path=None):
    """
    Clear the task registry (useful for testing)

    Args:
        file_path: If specified, only clear tasks from this file.
                  If None, clear all tasks from all files.
    """
    script_manager = get_script_manager()
    script_manager.clear_tasks(file_path)


def _generate_execution_metadata(function_args: dict, caller_frame, meta_dir):
    """Generate a YAML file with execution metadata"""
    filename = caller_frame.f_code.co_filename

    # Get path relative to FORGE home directory (forge root)
    rel_filename = _get_forge_relative_path(filename)

    # Use parent directory name as function name for toolbox operations
    function_name = _get_toolbox_function_name(filename)

    metadata = {
        "execution_metadata": {
            "file": str(rel_filename),
            "timestamp": datetime.now().isoformat(),
            "artifact_dir": str(env.ARTIFACT_DIR),
            "working_directory": str(Path.cwd()),
            "command": function_name,
            "arguments": {},
        }
    }

    # Add function arguments, filtering out internal ones
    for key, value in function_args.items():
        if key not in ["function_args"]:  # Skip internal parameters
            metadata["execution_metadata"]["arguments"][key] = value

    # Write metadata to YAML file
    metadata_file = meta_dir / "metadata.yaml"
    with open(metadata_file, "w") as f:
        yaml.dump(metadata, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Generated execution metadata: {metadata_file}")


def _generate_env_file(meta_dir):
    """Generate a file with environment variables as key/value pairs"""
    env_file = meta_dir / "env.txt"

    with open(env_file, "w") as f:
        for key, value in sorted(os.environ.items()):
            f.write(f"{key}={value}\n")

    logger.debug(f"Generated environment file: {env_file}")


def _generate_context_file(shared_context, meta_dir):
    """Generate a YAML file with the final shared context"""
    context_file = meta_dir / "context.yaml"

    # Convert SimpleNamespace to dict, filtering out private attributes
    context_dict = {}
    for key, value in vars(shared_context).items():
        if not key.startswith("_"):
            # Convert Path objects to strings for YAML serialization
            if hasattr(value, "__fspath__"):  # Path-like objects
                context_dict[key] = str(value)
            else:
                context_dict[key] = value

    context_data = {
        "final_context": context_dict,
        "timestamp": datetime.now().isoformat(),
    }

    with open(context_file, "w") as f:
        yaml.dump(context_data, f, default_flow_style=False, sort_keys=False)

    logger.debug(f"Generated context file: {context_file}")


# Thread-local storage for DSL logger handlers
_thread_local_handlers = threading.local()


class ThreadLocalHandler(logging.Handler):
    """A logging handler that routes messages to thread-specific files"""

    def __init__(self):
        super().__init__()

    def emit(self, record):
        # Only emit if we have a thread-local file handler for this thread
        if hasattr(_thread_local_handlers, "file_handler"):
            try:
                _thread_local_handlers.file_handler.emit(record)
            except Exception:
                # Ignore errors in logging to avoid breaking execution
                pass


def _setup_execution_logging(artifact_dir):
    """Setup thread-safe file logging to capture all stdout/stderr during execution"""
    log_file = artifact_dir / "task.log"

    # Create file handler for this specific execution
    file_handler = logging.FileHandler(log_file, mode="w")
    file_handler.setLevel(logging.INFO)

    # Use same format as console output
    file_handler.setFormatter(logging.Formatter("%(message)s"))

    # Store the file handler in thread-local storage
    _thread_local_handlers.file_handler = file_handler

    # Add thread-local handler to main DSL logger only once (globally)
    main_dsl_logger = logging.getLogger("DSL")

    # Check if our thread-local handler is already added
    has_thread_handler = any(isinstance(h, ThreadLocalHandler) for h in main_dsl_logger.handlers)

    if not has_thread_handler:
        thread_handler = ThreadLocalHandler()
        thread_handler.setLevel(logging.INFO)
        main_dsl_logger.addHandler(thread_handler)

    return log_file, file_handler


def _split_always_task_logs(artifact_dir):
    """Split @always task logs from main task log after execution completes"""
    task_log = artifact_dir / "task.log"
    always_log = artifact_dir / "task-always.log"
    marker = "=== @ALWAYS TASK EXECUTION BEGIN ==="

    try:
        if not task_log.exists():
            return

        # Read the full log content
        with open(task_log, encoding="utf-8") as f:
            content = f.read()

        # Find the marker
        marker_pos = content.find(marker)
        if marker_pos == -1:
            return  # No @always tasks were executed

        # Split content at marker
        main_content = content[:marker_pos].rstrip()
        always_content = content[marker_pos:].lstrip()

        # Write @always content to separate file
        with open(always_log, "w", encoding="utf-8") as f:
            f.write(always_content)

        # Truncate main log to remove @always content
        with open(task_log, "w", encoding="utf-8") as f:
            f.write(main_content)
            if main_content and not main_content.endswith("\n"):
                f.write("\n")

        logger.info(f"Split @always task logs to: {always_log}")

    except Exception as e:
        logger.warning(f"Failed to split @always task logs: {e}")
        # Don't fail the entire execution if log splitting fails


def _generate_restart_script(function_args: dict, caller_frame, meta_dir):
    """Generate a shell script to restart the execution with same parameters"""
    filename = caller_frame.f_code.co_filename
    rel_filename = _get_forge_relative_path(filename)

    script_content = "#!/bin/bash\n"
    script_content += "# Auto-generated restart script\n"
    script_content += f"# Generated on: {datetime.now().isoformat()}\n"
    script_content += f"# Original execution artifact dir: {env.ARTIFACT_DIR}\n\n"

    # Build command line with arguments on separate lines
    script_content += f'exec python "{rel_filename}"'

    # Add arguments, each on a new line with proper indentation
    for key, value in function_args.items():
        if (
            key not in ["function_args"] and value is not None
        ):  # Skip internal parameters and None values
            if isinstance(value, bool):
                if value:  # Only add flag if True
                    script_content += " \\\n    " + f"--{key.replace('_', '-')}"
            else:
                script_content += " \\\n    " + f'--{key.replace("_", "-")} "{value}"'

    script_content += "\n"

    # Write restart script
    restart_file = meta_dir / "restart.sh"
    with open(restart_file, "w") as f:
        f.write(script_content)

    # Make executable
    os.chmod(restart_file, 0o755)

    logger.debug(f"Generated restart script: {restart_file}")


def _generate_failure_file_for_agent(exception: Exception) -> None:
    """Generate a FAILURE file for agent analysis when task execution fails"""
    try:
        # Get the artifact directory - should be available in DSL execution context
        artifact_dir = env.ARTIFACT_DIR
        if not artifact_dir:
            logger.warning("No ARTIFACT_DIR available - cannot generate FAILURE file for agent")
            return

        failure_file_path = artifact_dir / "FAILURE"

        # Generate failure content
        failure_content = _format_failure_content_for_agent(exception)

        # Write FAILURE file
        with open(failure_file_path, "w", encoding="utf-8") as f:
            f.write(failure_content)

        logger.info(f"Generated FAILURE file for agent analysis: {failure_file_path}")

    except Exception as failure_error:
        logger.warning(f"Failed to generate FAILURE file for agent: {failure_error}")


def _format_failure_content_for_agent(exception: Exception) -> str:
    """Format exception information for FAILURE file that agent can analyze"""
    failure_lines = []
    failure_lines.append("=" * 80)
    failure_lines.append("TASK EXECUTION FAILURE")
    failure_lines.append("=" * 80)
    failure_lines.append(f"Timestamp: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    failure_lines.append(f"Exception Type: {exception.__class__.__name__}")
    failure_lines.append(f"Exception Message: {str(exception)}")
    failure_lines.append("")

    # Add TaskExecutionError details if available
    if isinstance(exception, TaskExecutionError):
        failure_lines.append("TASK EXECUTION DETAILS:")
        failure_lines.append(f"  Task Name: {exception.task_name}")
        failure_lines.append(f"  Task Description: {exception.task_description}")
        failure_lines.append(f"  Task Location: {exception.task_location}")
        failure_lines.append(f"  Artifact Directory: {exception.artifact_dir}")
        if exception.original_exception:
            failure_lines.append(
                f"  Original Exception: {exception.original_exception.__class__.__name__}"
            )
            failure_lines.append(f"  Original Message: {str(exception.original_exception)}")
        failure_lines.append("")

    # Add environment context
    failure_lines.append("ENVIRONMENT CONTEXT:")
    if env.ARTIFACT_DIR:
        failure_lines.append(f"  Artifact Directory: {env.ARTIFACT_DIR}")
    failure_lines.append("")

    # Add stack trace
    failure_lines.append("STACK TRACE:")
    failure_lines.append("-" * 40)
    import traceback

    failure_lines.append(traceback.format_exc())

    failure_lines.append("=" * 80)

    return "\n".join(failure_lines)
