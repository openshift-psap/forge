---
name: idd.suggest
description: >
  Propose updates to an existing .intent.md contract. Analyzes the current code and intent
  to identify stale constraints, missing API entries, undeclared side effects, or scope changes,
  then applies the edits directly. Use when drift is detected, verification fails, or the
  intent file needs updating to match evolved code.
argument-hint: <path/to/file.py or file.intent.md>
arguments: target_file
---

# IDD: Suggest Intent Changes

Propose and apply updates to an existing `.intent.md` contract file.

## User Input

```text
$target_file
```

## Reference Documents

Before proceeding, read these files for context:

1. Read `docs/intent_driven_development__template.md` — the required structure
2. Read `docs/intent_driven_development.md` — the IDD philosophy and rules
3. Read the repository-root `AGENTS.md` — project-wide rules, including the
   architectural layering and boundary constraints

## Procedure

### Step 1: Resolve the target pair and architectural layer

Parse `$target_file`. Accept either a `.py` or `.intent.md` path.

Resolve the 1-to-1 mapping:
- If given `foo.py`, look for `foo.intent.md` in the same directory
- If given `foo.intent.md`, the target is `foo.py` in the same directory

If the `.intent.md` file does not exist, stop and suggest
`/idd.create` instead.  If the `.py` file does not exist, stop and
tell the user that comparison requires an implementation. Suggest
`/idd.compile` to generate it.

Then resolve the target's architectural layer from its directory path (e.g.,
`projects/*/orchestration/`, `projects/*/toolbox/`, `projects/caliper/`) using
the layering rules in `AGENTS.md`. Together with the module-level rules loaded
in Step 2, this determines the mandatory constraints below. Both must be loaded
before classifying constraints or editing the intent file.

### Step 2: Read both files

Read the existing `.intent.md` contract and parse all sections:

1. **Core Intent** — the declared business goal
2. **Public API Surface** — declared functions/classes/exceptions
3. **Side Effects & State** — declared external interactions
4. **Constraints** — absolute boundaries
5. **Verification Hooks** — required tests

Read the corresponding `.py` file and extract:

1. All public symbols (functions, classes, exceptions — not `_`-prefixed)
2. All imports
3. Side effects (file I/O, network, global state mutation, subprocess calls)
4. `[AGENT-LOCK]` block locations

Also check for `index.intent.md` in the same directory and include its module-level rules.

### Step 3: Identify changes needed

Compare the intent contract against the actual code. Identify:

- **Stale API Surface**: Public functions/classes in the code but not listed in the intent,
  or listed in the intent but removed from the code
- **Undeclared Side Effects**: The code has side effects not covered by Side Effects & State
- **Missing Constraints**: Patterns in the code that imply constraints not yet documented
  (e.g., the code carefully avoids certain imports — that should be a constraint)
- **Outdated Constraints**: Constraints that no longer match how the code actually works,
  because the code has legitimately evolved
- **Stale Verification Hooks**: Test references that point to nonexistent or renamed test files
- **Core Intent Drift**: The file's actual purpose has shifted from what Core Intent describes

When evaluating Constraints, separate mandatory architectural boundaries from
local constraints:

- **Mandatory architectural boundaries** — import restrictions, required imports,
  and required entrypoints imposed on the target by its layer (per `AGENTS.md`),
  its module's `index.intent.md`, or other repo rules. These hold regardless of
  how the code has evolved: never classify them as outdated, never remove or
  relax them, and add them if missing from the intent.
- **Local constraints** — file-specific boundaries. Only these may become
  "Outdated Constraints" when the code has legitimately evolved.

### Step 4: Apply changes

Edit the `.intent.md` file directly using the Edit tool. For each change:

1. Preserve the required section structure (Core Intent, Public API Surface,
   Side Effects & State, Constraints, Verification Hooks)
2. Keep existing content that is still accurate
3. Add new entries where gaps were found
4. Remove or update entries that are stale
5. Do not rewrite sections that don't need changes
6. Never remove or relax a mandatory architectural boundary constraint — including
   required imports and entrypoints for orchestration or toolbox targets. If the
   code violates one, flag the code (not the intent) for fixing, via `/idd.compile`
   or a direct code change

If the Core Intent needs the word "and" after the update, warn that the file may need
splitting (the "And" test).

### Step 5: Validate

Run `scripts/validate_intent_files.py` on the modified `.intent.md` file to confirm
structural correctness.

### Step 6: Report

Report:
- What sections were changed and why
- Whether any changes were skipped (and why — e.g., ambiguous, needs human judgment)
- Whether the "And" test flagged a potential SRP violation
- Suggest `/idd.compile` if the code should be regenerated from the updated intent
- Suggest `/idd.drift` to verify the updated intent is now in sync
