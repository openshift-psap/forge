# Intent-Driven Development (IDD) in FORGE

## Philosophy
To maintain control of the FORGE codebase as we integrate AI coding
agents, we treat **Intent** as the primary artifact and source code as
a transient byproduct. We manage the architecture through declarative,
constraint-based English specifications (`.intent.md` files) that
agents compile into Python.

If an agent generates incorrect code, **we do not manually fix the
code.** We tighten the constraints in the intent file and force the
agent to regenerate it. Conversely, if an agent detects that the
intent file is stale — the code has legitimately evolved and the
contract no longer matches — the agent can propose updates to the
intent file via `/idd.suggest`.

## The Golden Rule: One File, One Contract
There is a strict 1-to-1 mapping between a source file and its intent
file (e.g., `my_parser.py` is governed exclusively by
`my_parser.intent.md`).

AI agents degrade in precision as file sizes grow. To enforce small,
atomic modules, use the intent files as a gauge for when to refactor:
* **The "And" Test:** If the *Core Intent* requires the word "and"
  (e.g., "Parses the artifact *and* formats it *and* manages
  retries"), the Python file is violating the Single Responsibility
  Principle and must be split.
* **Constraint Bloat:** If you need more than 10-15 bullet points in
  the *Constraints* section to wall off the agent from breaking
  things, the file's surface area is too large.
* **Legacy Refactoring:** When bringing a legacy 1,000-line file into
  the IDD workflow, do not write a massive intent file for
  it. Instead, write an intent file that instructs an agent to extract
  a specific piece of logic into a new, separate module.

## File Anatomy
Every file-level `.intent.md` must strictly contain the following
sections:

1. **YAML Frontmatter:** Machine-readable boundaries defining
   `target_file` and `allowed_imports`.
2. **Core Intent:** A strict 1-2 sentence definition of the business
   goal.
3. **Data Contracts:** Exact input parameters, data types, and output
   shapes.
4. **Constraints:** Unordered bullet points starting with restrictive
   verbs ("Do not", "Never", "Reject"). This is the primary defense
   against AI hallucination and scope creep.
5. **Verification Hooks:** Explicit testing strategies the agent must
   implement (e.g., specific `pytest` coverage) to prove the contract
   is satisfied.

## Human & Agent Coexistence
Human maintainers and AI agents share this codebase cooperatively.
Both may propose changes to both code and intent files. We use
specific mechanisms to keep the two in sync and to protect
highly optimized human-written logic from unintended refactoring.

### 1. Drift Detection (CI)
When anyone — human or agent — modifies a `.py` file that has an
associated `.intent.md` file, the CI pipeline will run a drift check.
If the new code violates the existing English constraints, or
introduces new scope not covered by the intent file, the PR will be
flagged for review. The same applies in reverse: when an intent file
changes, drift detection verifies the code still matches.

### 2. Surgical Preservation
Agents are bound by `AGENTS.md` to surgically preserve existing logic,
variables, and algorithms when modifying a file, unless that logic
explicitly violates the intent contract.

### 3. Agent-Lock Blocks
If a human maintainer writes highly complex, fragile, or manually
optimized code (e.g., complex matrix calculations or specific
C-bindings) that an agent should never attempt to refactor, fence it
off using standard lock comments:

```python
# [AGENT-LOCK: START] - Optimized iteration, do not refactor
def parse_complex_matrix():
    ...
# [AGENT-LOCK: END]

Agents will read this logic for context but are strictly forbidden
from modifying it.

## Skills

Three slash commands support the IDD workflow:

### `/idd.create <path/to/file.py>`
Scaffold a `.intent.md` contract for a Python file. If the `.py` file
exists, it analyzes the code to pre-populate the API surface, side
effects, and layer-appropriate constraints. If the file doesn't exist
yet, it creates a blank template for design-first development.

### `/idd.compile <path/to/file.py or file.intent.md>`
Generate or update a `.py` file from its `.intent.md` contract.
Preserves `[AGENT-LOCK]` blocks, respects architectural layer
boundaries and all constraints, then runs verification hooks. Supports
both fresh generation and surgical updates to existing code.

### `/idd.suggest <path/to/file.py or file.intent.md>`
Propose and apply updates to an existing `.intent.md` contract.
Analyzes the current code to identify stale constraints, missing API
entries, undeclared side effects, or scope changes, then edits the
intent file directly. Use when drift is detected, verification fails,
or the intent needs updating to match evolved code.

### `/idd.drift [path/to/file.py]`
Compare Python files against their `.intent.md` contracts. Reports
findings in four categories:
- **VIOLATION** — constraint breach or undeclared side effect
- **SCOPE CREEP** — code exists that the intent doesn't cover
- **MISSING** — intent declares something the code doesn't implement
- **STALE** — verification hooks with no corresponding tests

Without an argument, scans all `.intent.md` files in the current
directory tree.
