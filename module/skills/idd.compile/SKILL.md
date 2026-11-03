---
name: idd.compile
description: >
  Generate or update a Python file from its .intent.md contract. Respects agent-lock
  blocks, architectural boundaries, and verification hooks. Use when the user wants to
  compile, generate, regenerate, or update code from an intent file.
argument-hint: <path/to/file.py or file.intent.md>
arguments: target_file
---

# IDD: Compile Intent to Code

Generate or regenerate a Python file from its `.intent.md` contract.

## User Input

```text
$target_file
```

## Reference Documents

Before proceeding, read these files:

1. Read `docs/intent_driven_development.md` — the IDD philosophy and rules
2. Read the repository-root `AGENTS.md` — project-wide rules (IDD system directive, secret handling, error handling, etc.)

## Procedure

### Step 1: Resolve the target pair

Parse `$target_file`. Accept either a `.py` or `.intent.md` path.

Resolve the 1-to-1 mapping:
- If given `foo.py`, look for `foo.intent.md` in the same directory
- If given `foo.intent.md`, the target is `foo.py` in the same directory

If the `.intent.md` file does not exist, stop and tell the user. Suggest `/idd.create` first.

### Step 2: Read the intent contract

Read the `.intent.md` file and parse all sections:

1. **Core Intent** — the business goal; this defines WHAT the code must do
2. **Public API Surface** — the exact functions/classes to expose
3. **Side Effects & State** — what the code is allowed to touch outside its scope
4. **Constraints** — absolute boundaries; negative constraints take precedence over everything
5. **Verification Hooks** — tests that must be written/pass

### Step 3: Check for module-level rules

Look for an `index.intent.md` in the same directory. If it exists, read it and apply its
module-level boundaries as additional constraints. Module rules are additive — they restrict,
never expand, what a file-level intent allows.

### Step 4: Determine architectural constraints

From the file path, identify the architectural layer and its import rules:

- **Orchestration** (`projects/*/orchestration/`): May import from `projects/*/toolbox/` and `projects/caliper/orchestration`. Public exports only.
- **Toolbox** (`projects/*/toolbox/`): Standard library and external pip only. Never import from orchestration or library.
- **Caliper** (`projects/caliper/`): Never import from other `project/*`.

These constraints are implicit and apply even if not stated in the intent file.

### Step 5: Generate or update the Python file

**If the `.py` file does NOT exist** — generate it fresh:

1. Create the file with only the public API declared in the intent
2. Implement each function/class to satisfy the Core Intent
3. Use only imports allowed by the architectural layer and any explicit `allowed_imports`
4. Respect all Constraints — negative constraints are non-negotiable
5. Do not add undocumented features, premature abstractions, or scope beyond the Core Intent

**If the `.py` file EXISTS** — surgical update:

1. Read the existing code completely
2. Identify `[AGENT-LOCK: START]` / `[AGENT-LOCK: END]` blocks — preserve them verbatim,
   do not modify any code within these fences
3. Compare existing code against the intent contract:
   - Add missing functions/classes from Public API Surface
   - Fix implementations that violate Constraints
   - Remove functions not in Public API Surface only if they are clearly unused
     (not called by any locked or public code)
4. Preserve existing logic, variable names, and algorithms that don't violate the contract
5. Do not rewrite working code just to match a different style

### Step 6: Verify constraints compliance

After generation, self-check the produced code:

1. Every function in Public API Surface exists in the code
2. No imports violate architectural layer rules
3. No negative Constraints are violated
4. Side effects match what Side Effects & State declares
5. No scope creep — no undocumented public functions or classes

If any check fails, fix the code before proceeding.

### Step 7: Run verification hooks

If the Verification Hooks section specifies tests:

1. Check if corresponding test files exist
2. If tests exist, run them and report results
3. If tests are specified but don't exist, generate them according to the hooks' instructions
4. Run the generated tests
5. The compilation is not complete until all verification hooks pass

### Step 8: Report

Report:
- Whether the file was created fresh or updated
- What functions/classes were generated or modified
- Any Agent-Lock blocks that were preserved
- Verification hook results (pass/fail)
- Any warnings about potential issues

If verification hooks fail, show the failures and offer three options:
1. Iterate on the code to satisfy the current intent
2. Run `/idd.suggest` to propose updates to the intent file
3. Stop and let the user decide
