---
name: idd.drift
description: >
  Check for drift between Python files and their .intent.md contracts. Reports
  constraint violations, scope creep, missing implementations, and stale verification
  hooks. Use when the user wants to check drift, verify intent compliance, or audit
  code against its contract.
argument-hint: "[path/to/file.py]"
arguments: target_file
---

# IDD: Drift Check

Compare Python files against their `.intent.md` contracts and report violations.

## User Input

```text
$target_file
```

## Reference Documents

Before proceeding, read:

1. Read `docs/intent_driven_development.md` — the IDD philosophy and rules
2. Read the repository-root `AGENTS.md` — project-wide rules

## Procedure

### Step 1: Discover file pairs

**If `$target_file` is provided:**

Accept either a `.py` or `.intent.md` path.

If the target is an `index.intent.md`, treat it as a module-level contract — it has
no paired `.py` file. Apply the same handling as the no-argument scan: read its
module-level rules (Step 2) and check the `.py` files in the same directory against
them, classifying breaches as VIOLATION findings (Step 4). Do not report it as an
orphan.

For any other target, resolve the 1-to-1 pair:
- `foo.py` → look for `foo.intent.md`
- `foo.intent.md` → look for `foo.py`

If the counterpart doesn't exist, report it as an orphan:
- `.py` without `.intent.md`: "No intent contract found — run `/idd.create` to add one"
- `.intent.md` without `.py`: "No implementation found — run `/idd.compile` to generate it"

**If `$target_file` is empty:**

Scan the current working directory tree for all `.intent.md` files.
Skip `index.intent.md` files. They hold module-level rules and have no paired `.py` file. Step 2 reads them.
For each remaining file, resolve the paired `.py` file. Report orphans and then analyze all valid pairs.

### Step 2: Parse the intent contract

For each `.intent.md` file, parse:

1. **Core Intent** — the declared business goal
2. **Public API Surface** — declared functions/classes/exceptions
3. **Side Effects & State** — declared external interactions
4. **Constraints** — absolute boundaries
5. **Verification Hooks** — required tests

Also check for `index.intent.md` in the same directory and include its module-level rules.

### Step 3: Analyze the Python file

Read the corresponding `.py` file and extract:

1. All public symbols (functions, classes, exceptions — not `_`-prefixed)
2. All imports
3. Side effects (file I/O, network, global state mutation, subprocess calls)
4. `[AGENT-LOCK]` block locations
5. Whether test files exist for the verification hooks

### Step 4: Compare and classify findings

Compare the intent contract against the actual code. Classify each finding:

#### VIOLATION (constraint breach)
- A negative constraint ("Do not", "Never") is violated in the code
- An import exists that the architectural layer forbids
- Side effects occur that are not declared in Side Effects & State
- Agent-Lock blocks have been modified (compare against git history if available)

#### SCOPE CREEP (undeclared code)
- Public functions or classes exist in the code but are not listed in Public API Surface
- The code implements behavior beyond what Core Intent describes

#### MISSING (incomplete implementation)
- Functions or classes declared in Public API Surface don't exist in the code
- Core Intent describes behavior the code doesn't implement

#### STALE (verification gap)
- Verification Hooks specify tests that don't exist as test files
- Test files exist but don't cover what the hooks describe

### Step 5: Report findings

Format the report:

```
## IDD Drift Report: <filename>

### Summary
<N> findings: <X> violations, <Y> scope creep, <Z> missing, <W> stale

### Findings

#### VIOLATION
- <file>:<line>: <description>
  **Fix:** <suggestion — update code or intent>

#### SCOPE CREEP
- <file>:<line>: `function_name()` not declared in Public API Surface
  **Fix:** Add to intent file or remove from code

#### MISSING
- <intent_file>: `function_name()` declared but not implemented
  **Fix:** Implement in code or remove from intent

#### STALE
- <intent_file>: Verification hook "<hook>" has no corresponding test
  **Fix:** Write tests or update verification hooks

### Verdict
✅ IN SYNC — code matches its intent contract
⚠️  DRIFT DETECTED — <N> findings need attention
🚫 CONTRACT BROKEN — <N> constraint violations must be fixed
```

### Step 6: Suggest actions

For each finding, recommend one of:
- **Update the intent** — when the code is correct and the intent is outdated
  (use `/idd.suggest` to apply changes)
- **Update the code** — when the intent is correct and the code has drifted
  (use `/idd.compile` to regenerate)
- **Split the file** — when scope creep indicates an SRP violation
  (the "And" test from the IDD philosophy)

If the user wants to fix drift, offer to:
1. Run `/idd.suggest` to update the intent file (for legitimate scope changes)
2. Apply code fixes directly (for constraint violations)
3. Run `/idd.compile` to regenerate the code from the corrected intent
