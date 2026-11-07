# Intent: [filename.py]

## Core Intent
[1-2 sentences strictly defining the business goal of this specific file. If you need the word "and" to describe it, consider splitting the Python file.]

## Public API Surface
[List the classes, functions, or exceptions this module exposes to the rest of the codebase. Do not define type hints here; use Python's native `typing` in the code itself.]
- `function_name()`
- `ClassName`

## Side Effects & State
[Define what this module is allowed to touch outside its own scope. E.g., "Stateless", "Reads from disk", "Mutates global config", "Makes external API calls".]
-

## Constraints
[The absolute boundaries for the AI agent. Use unordered bullet points starting with restrictive verbs like "Do not", "Never", "Prevent".]
- Do not...
- Never...

## Verification Hooks
[Explicit testing requirements the agent must implement to prove the contract is satisfied. E.g., "Write property-based tests for X", "Mock the filesystem to verify Y."]
-
