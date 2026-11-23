"""
sitecustomize.py - Auto-loaded PyTorch profiler for vLLM workers

This module is automatically loaded by Python when it starts (via PYTHONPATH).
It installs an import hook that intercepts vllm.v1.worker.gpu_worker module
loading and wraps Worker.execute_model with torch.profiler instrumentation.

The profiler records CPU+CUDA activity for configured call ranges, then exports
Chrome trace JSON files for visualization.

Configuration sources (in priority order):
1. Environment variables (e.g., VLLM_PROFILER_RANGES="50-100,200-300")
2. profiler_config.yaml file (if present)
3. Hardcoded defaults
"""
import sys
import os
import importlib
import importlib.util
import importlib.abc
from typing import List, Tuple, Optional

# ==============================================================================
# vLLM Configuration
# ==============================================================================

# Set vLLM RPC timeout (in milliseconds)
os.environ.setdefault('VLLM_RPC_TIMEOUT', '1800000')

# ==============================================================================
# Configuration Management
# ==============================================================================

class ProfilerConfig:
    """Manages profiler configuration from multiple sources."""

    def __init__(self):
        self.ranges: List[Tuple[int, int]] = []
        self.activities: List[str] = ["CPU", "CUDA"]
        self.record_shapes: bool = True
        self.with_stack: bool = True
        self.profile_memory: bool = False
        self.with_modules: bool = False
        self.export_chrome_trace: bool = True
        self.output_file_pattern: str = "trace_pid{pid}.json"
        self.table_enabled: bool = True
        self.table_sort_by: str = "cuda_time_total"
        self.table_row_limit: int = 50
        self.print_stats: bool = True
        # Support multiple target modules for different vLLM versions
        # Format: [(module, class, method), ...]
        self.target_modules: List[Tuple[str, str, str]] = [
            ("vllm.v1.worker.gpu_worker", "Worker", "execute_model"),  # vLLM >= 0.12
            ("vllm.worker.worker", "Worker", "execute_model"),          # vLLM 0.11.x
        ]
        # Legacy single target (for backward compatibility with config files)
        self.target_module: str = "vllm.v1.worker.gpu_worker"
        self.target_class: str = "Worker"
        self.target_method: str = "execute_model"
        self.debug: bool = False

        self._load_config()

    def _load_config(self):
        """Load configuration from environment variables and config file."""
        # First, try to load from YAML file
        self._load_from_yaml()

        # Then override with environment variables (highest priority)
        self._load_from_env()

        # Validate and parse ranges
        if not self.ranges:
            # Default range if none specified
            self.ranges = [(100, 150)]

        if self.debug:
            print(f"[profiler-config] Loaded configuration:")
            print(f"  Ranges: {self.ranges}")
            print(f"  Activities: {self.activities}")
            print(f"  Output: {self.output_file_pattern}")

    def _load_from_yaml(self):
        """Load configuration from profiler_config.yaml if present."""
        config_path = os.path.join(
            os.path.dirname(__file__) or "/home/vllm/profiler",
            "profiler_config.yaml"
        )

        if not os.path.exists(config_path):
            return

        try:
            import yaml
            with open(config_path, 'r') as f:
                config = yaml.safe_load(f)

            if config:
                # Parse profiling ranges
                if 'profiling_ranges' in config:
                    self.ranges = self._parse_ranges(config['profiling_ranges'])

                # Activities
                if 'activities' in config:
                    self.activities = [a.strip() for a in config['activities'].split(',')]

                # Options
                opts = config.get('options', {})
                self.record_shapes = opts.get('record_shapes', self.record_shapes)
                self.with_stack = opts.get('with_stack', self.with_stack)
                self.profile_memory = opts.get('profile_memory', self.profile_memory)
                self.with_modules = opts.get('with_modules', self.with_modules)

                # Output configuration
                output = config.get('output', {})
                self.export_chrome_trace = output.get('export_chrome_trace', self.export_chrome_trace)
                self.output_file_pattern = output.get('file_pattern', self.output_file_pattern)
                self.print_stats = output.get('print_stats', self.print_stats)

                table = output.get('table', {})
                self.table_enabled = table.get('enabled', self.table_enabled)
                self.table_sort_by = table.get('sort_by', self.table_sort_by)
                self.table_row_limit = table.get('row_limit', self.table_row_limit)

                # Advanced settings
                adv = config.get('advanced', {})
                self.target_module = adv.get('target_module', self.target_module)
                self.target_class = adv.get('target_class', self.target_class)
                self.target_method = adv.get('target_method', self.target_method)
                self.debug = adv.get('debug', self.debug)

        except ImportError:
            # PyYAML not available, skip file-based config
            pass
        except Exception as e:
            print(f"[profiler-config] Warning: Failed to load config file: {e}")

    def _load_from_env(self):
        """Load configuration from environment variables."""
        # Profiling ranges
        if 'VLLM_PROFILER_RANGES' in os.environ:
            self.ranges = self._parse_ranges(os.environ['VLLM_PROFILER_RANGES'])

        # Activities
        if 'VLLM_PROFILER_ACTIVITIES' in os.environ:
            self.activities = [a.strip() for a in os.environ['VLLM_PROFILER_ACTIVITIES'].split(',')]

        # Options
        if 'VLLM_PROFILER_RECORD_SHAPES' in os.environ:
            self.record_shapes = os.environ['VLLM_PROFILER_RECORD_SHAPES'].lower() in ('true', '1', 'yes')

        if 'VLLM_PROFILER_WITH_STACK' in os.environ:
            self.with_stack = os.environ['VLLM_PROFILER_WITH_STACK'].lower() in ('true', '1', 'yes')

        if 'VLLM_PROFILER_MEMORY' in os.environ:
            self.profile_memory = os.environ['VLLM_PROFILER_MEMORY'].lower() in ('true', '1', 'yes')

        # Output file pattern
        if 'VLLM_PROFILER_OUTPUT' in os.environ:
            self.output_file_pattern = os.environ['VLLM_PROFILER_OUTPUT']

        # Chrome trace export
        if 'VLLM_PROFILER_EXPORT_TRACE' in os.environ:
            self.export_chrome_trace = os.environ['VLLM_PROFILER_EXPORT_TRACE'].lower() in ('true', '1', 'yes')

        # Debug mode
        if 'VLLM_PROFILER_DEBUG' in os.environ:
            self.debug = os.environ['VLLM_PROFILER_DEBUG'].lower() in ('true', '1', 'yes')

    def _parse_ranges(self, ranges_str: str) -> List[Tuple[int, int]]:
        """
        Parse profiling ranges from string format.

        Examples:
            "100-150" -> [(100, 150)]
            "50-100,200-300" -> [(50, 100), (200, 300)]
            "0-50,100-150,300-350" -> [(0, 50), (100, 150), (300, 350)]
        """
        ranges = []
        for range_str in ranges_str.split(','):
            range_str = range_str.strip()
            if '-' in range_str:
                try:
                    start, end = range_str.split('-')
                    ranges.append((int(start), int(end)))
                except ValueError as e:
                    print(f"[profiler-config] Warning: Invalid range '{range_str}': {e}")
        return ranges

    def get_output_filename(self, pid: Optional[int] = None, rank: Optional[int] = None, 
                            range_start: Optional[int] = None, range_end: Optional[int] = None,
                            run_id: Optional[str] = None) -> str:
        """Generate output filename with substitutions."""
        filename = self.output_file_pattern
        filename = filename.replace('{pid}', str(pid or os.getpid()))
        
        # Try to get rank from various sources if not provided
        if rank is None:
            # Try torch.distributed first
            try:
                import torch.distributed as dist
                if dist.is_initialized():
                    rank = dist.get_rank()
            except:
                pass
            
            # Fall back to environment variables
            if rank is None:
                rank = os.environ.get('LOCAL_RANK') or os.environ.get('RANK')
                if rank is not None:
                    rank = int(rank)
        
        if rank is not None:
            filename = filename.replace('{rank}', str(rank))
        
        # Build suffix: _run{id}_range{start}-{end} before .json
        suffix = ''
        if run_id is not None:
            suffix += f'_run{run_id}'
        if range_start is not None and range_end is not None:
            suffix += f'_range{range_start}-{range_end}'
        if suffix:
            if filename.endswith('.json'):
                filename = filename[:-5] + suffix + '.json'
            else:
                filename = filename + suffix
        
        return filename


# Global configuration instance
_config = ProfilerConfig()


# ==============================================================================
# Import Hook
# ==============================================================================

class PostImportLoader(importlib.abc.Loader):
    def __init__(self, loader):
        self.loader = loader

    def create_module(self, spec):
        if hasattr(self.loader, "create_module"):
            return self.loader.create_module(spec)
        return None

    def exec_module(self, module):
        self.loader.exec_module(module)
        if _config.debug:
            print(f"[profiler] {module.__name__} loaded")
        safe_wrap_function(module)
        if _config.debug:
            print(f"[profiler] {module.__name__} wrapped")


class PostImportFinder(importlib.abc.MetaPathFinder):
    def __init__(self):
        # Build set of target modules to watch
        self.target_modules = set()
        for module, cls, method in _config.target_modules:
            self.target_modules.add(module)
        # Also add legacy single target
        self.target_modules.add(_config.target_module)
    
    def find_spec(self, fullname, path, target=None):
        if fullname not in self.target_modules:
            return None

        # Prevent recursive lookup
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)

        if spec and spec.loader:
            spec.loader = PostImportLoader(spec.loader)
            return spec
        return None


# Install the import hook
sys.meta_path.insert(0, PostImportFinder())


# ==============================================================================
# Profiler Wrapper
# ==============================================================================

def wrap_func_with_profiler(original_func):
    """
    Wraps a function with PyTorch profiler that activates for configured ranges.

    Supports multiple profiling windows, e.g., calls 50-100 and 200-300.
    """
    import torch
    import functools
    from torch.profiler import profile, ProfilerActivity

    # Parse activities
    activities = []
    for activity in _config.activities:
        if activity.upper() == "CPU":
            activities.append(ProfilerActivity.CPU)
        elif activity.upper() == "CUDA":
            activities.append(ProfilerActivity.CUDA)

    # Create profiler instance
    prof = profile(
        activities=activities,
        record_shapes=_config.record_shapes,
        with_stack=_config.with_stack,
        profile_memory=_config.profile_memory,
        with_modules=_config.with_modules
    )

    GATE_FILE = "/tmp/profiler_gate"

    # Track call count and current profiling range index
    count = 0
    current_range_idx = 0
    profiling_active = False
    last_gate_value = None

    @functools.wraps(original_func)
    def wrapped_func(*args, **kwargs):
        nonlocal count, current_range_idx, profiling_active, prof, last_gate_value

        gate_value = None
        try:
            with open(GATE_FILE) as f:
                gate_value = f.read().strip()
        except FileNotFoundError:
            pass

        if gate_value is None:
            last_gate_value = None
            result = original_func(*args, **kwargs)
            return result

        if gate_value != last_gate_value:
            count = 0
            current_range_idx = 0
            profiling_active = False
            prof = profile(
                activities=activities,
                record_shapes=_config.record_shapes,
                with_stack=_config.with_stack,
                profile_memory=_config.profile_memory,
                with_modules=_config.with_modules
            )
            print(f"[profiler] Gate activated (run={gate_value}) — counter reset, profiling ranges: {_config.ranges}", flush=True)
            last_gate_value = gate_value

        count += 1

        # Check if we should start profiling
        if not profiling_active and current_range_idx < len(_config.ranges):
            start, end = _config.ranges[current_range_idx]
            if count == start:
                print(f"[profiler] Starting profiler for range {start}-{end} (call #{count})", flush=True)
                prof.start()
                profiling_active = True

        # Check if we should stop profiling
        if profiling_active:
            start, end = _config.ranges[current_range_idx]
            if count == end:
                print(f"[profiler] Stopping profiler for range {start}-{end} (call #{count})", flush=True)
                prof.stop()
                profiling_active = False

                # Print and export results
                if _config.print_stats:
                    print("===== begin profiler output", flush=True)
                    if _config.table_enabled:
                        print(prof.key_averages().table(
                            sort_by=_config.table_sort_by,
                            row_limit=_config.table_row_limit
                        ), flush=True)
                    print("===== end profiler output", flush=True)

                # Optionally export Chrome trace file
                if _config.export_chrome_trace:
                    try:
                        output_file = _config.get_output_filename(
                            range_start=start, range_end=end, run_id=last_gate_value
                        )
                        prof.export_chrome_trace(output_file)
                        print(f"[profiler] Exported trace to: {output_file}", flush=True)
                    except Exception as e:
                        print(f"[profiler] ERROR exporting trace: {e}", flush=True)
                else:
                    print(f"[profiler] Chrome trace export disabled (export_chrome_trace=false)", flush=True)

                # Move to next range
                current_range_idx += 1

                # Create new profiler for next range if exists
                if current_range_idx < len(_config.ranges):
                    prof = profile(
                        activities=activities,
                        record_shapes=_config.record_shapes,
                        with_stack=_config.with_stack,
                        profile_memory=_config.profile_memory,
                        with_modules=_config.with_modules
                    )

        # Call original function
        result = original_func(*args, **kwargs)
        return result

    return wrapped_func


# ==============================================================================
# Helper Functions
# ==============================================================================

def safe_wrap_function(module=None):
    """Safely wrap the target function with error handling."""
    try:
        if module is None:
            return
        wrap_function(module)
    except Exception as e:
        print(f"[profiler] Error wrapping function: {e}")
        if _config.debug:
            import traceback
            traceback.print_exc()


def wrap_function(mod):
    """Wrap the target method with profiler."""
    module_name = mod.__name__
    
    # Find matching target config for this module
    target_class_name = None
    target_method_name = None
    
    for target_mod, cls, method in _config.target_modules:
        if target_mod == module_name:
            target_class_name = cls
            target_method_name = method
            break
    
    # Fall back to legacy single target
    if target_class_name is None and module_name == _config.target_module:
        target_class_name = _config.target_class
        target_method_name = _config.target_method
    
    if target_class_name is None:
        if _config.debug:
            print(f"[profiler] No target config found for module {module_name}")
        return
    
    target_class = getattr(mod, target_class_name, None)
    if target_class is None:
        print(f"[profiler] Warning: Class '{target_class_name}' not found in {module_name}")
        return

    original_method = getattr(target_class, target_method_name, None)
    if original_method is None:
        print(f"[profiler] Warning: Method '{target_method_name}' not found in {target_class_name}")
        return

    if _config.debug:
        print(f"[profiler] Wrapping {target_class_name}.{target_method_name}")

    setattr(target_class, target_method_name, wrap_func_with_profiler(original_method))
    print(f"[profiler] Successfully wrapped {module_name}.{target_class_name}.{target_method_name}")


def unwrap_function():
    """Remove profiler wrapping (for debugging)."""
    import vllm.v1.worker.gpu_worker
    vllm.v1.worker.gpu_worker.Worker.execute_model = \
        vllm.v1.worker.gpu_worker.Worker.execute_model.__wrapped__


# ==============================================================================
# Startup
# ==============================================================================

print(f"[profiler] vLLM profiler installed - will profile ranges: {_config.ranges}", file=sys.stderr, flush=True)
print(f"[profiler] Target: {_config.target_module}.{_config.target_class}.{_config.target_method}", file=sys.stderr, flush=True)
