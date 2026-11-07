---
name: idd.create
description: >
  Create a new .intent.md contract file for a Python file. Analyzes existing code
  to pre-populate API surface, side effects, and constraints. Use when the user wants
  to create an intent file, add IDD to a file, or scaffold a new intent contract.
argument-hint: <path/to/file.py>
arguments: target_file
---

# IDD: Create Intent File

Create a `.intent.md` contract file for the target Python file.

## User Input

```text
$target_file
```

## Reference Documents

Before proceeding, read these files for context:

1. Read `docs/intent_driven_development__template.md` — the template to use
2. Read `docs/intent_driven_development.md` — the IDD philosophy and rules

## Procedure

### Step 1: Resolve the target

Parse `$target_file` as the path to a Python file. If it doesn't end in `.py`, append `.py`.
Resolve it relative to the repository root.

The intent file path is the same base name with `.intent.md` instead of `.py`:
- `my_parser.py` → `my_parser.intent.md`

If the `.intent.md` file already exists, stop and tell the user. Suggest `/idd.suggest` to
propose updates or `/idd.drift` to check for drift.

### Step 2: Determine architectural layer

Inspect the file path to determine which layer the file belongs to:

- **Orchestration** (`projects/*/orchestration/`): May import from `projects/*/toolbox/` and `projects/caliper/orchestration`. Never import from other project orchestration or `projects/caliper/engine`.
- **Toolbox** (`projects/*/toolbox/`): Standalone utilities only. Never import from orchestration or library layers. All inputs via `run()` entrypoint parameters.
- **Caliper** (`projects/caliper/`): Never import from any other `project/*`.
- **Core DSL** (`projects/core/dsl/`): Shared DSL utilities.
- **Other**: No special layer constraints.

### Step 3: Analyze existing code (if the .py file exists)

If the target `.py` file exists, read it and extract:

1. **Public API Surface**: All public functions, classes, and exceptions (not prefixed with `_`).
   List each with its signature.
2. **Side Effects & State**: Identify what the module touches outside its own scope:
   - File I/O (reads/writes to disk)
   - Network calls (HTTP, subprocess calls to `oc`, `kubectl`, etc.)
   - Global state mutation (`os.environ`, module-level dicts, etc.)
   - If none found, mark as "Stateless"
3. **Existing imports**: Note what the file imports to inform constraint generation.
4. **Agent-Lock blocks**: Note any `[AGENT-LOCK: START/END]` blocks — these will be
   referenced in constraints.

### Step 4: Generate the intent file

Using the template from `intent_template.md`, create the `.intent.md` file:

1. **Core Intent**: If the `.py` exists, write 1-2 sentences summarizing the file's purpose
   based on its docstrings, function names, and behavior. Apply the "And" test — if you need
   "and" to describe it, warn the user the file may need splitting.
   If the `.py` doesn't exist, leave a placeholder: `[Define the business goal of this file.]`

2. **Public API Surface**: If the `.py` exists, list discovered functions/classes.
   If not, replace the example entries `function_name()` and `ClassName` with a bracketed placeholder such as `[List the functions and classes this file will expose.]`.

3. **Side Effects & State**: Populate from analysis or leave template placeholders.

4. **Constraints**: Always include layer-appropriate constraints from Step 2. Add file-specific
   constraints based on analysis (e.g., "Do not import from X", "Never log secret values").
   If Agent-Lock blocks exist, add: "Do not modify code within [AGENT-LOCK] blocks."

5. **Verification Hooks**: If the `.py` exists and has corresponding test files, reference them.
   Otherwise leave placeholders for the user to fill in.

### Step 5: Write and report

Write the `.intent.md` file to disk next to the target `.py` file.

Check if an `index.intent.md` exists in the same directory. If not, mention that the user
may want to create one to define module-level boundaries (but do not create it automatically).

Report:
- The path of the created intent file
- A summary of what was pre-populated vs. what needs manual review
- Whether the "And" test flagged a potential SRP violation
- Whether an `index.intent.md` is missing from the directory
