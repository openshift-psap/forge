# Hosted private serving images

This implementation targets Forge PR #322 and the matching Inference Playbooks
contract-v3 importer. Public recipes continue to use the existing v1/v2 contracts.

Declare `image_visibility: private` and `image_env: RHOAI_36_RC_IMAGE` directly in
the recipe's serving block or selected platform override. Commit only the logical
name and generated `private.invalid/runtime:RHOAI_36_RC_IMAGE` placeholders.
One Deployment, LeaderWorkerSet, or LLMInferenceService is supported.

## Hub setup

Provision the optional `psap-forge-private-images` vault through the existing
Fournos vault mounting mechanism. Its `images.json` file maps logical names to
private image references. `PSAP_FORGE_PRIVATE_IMAGES_SECRET_PATH` points at the
mounted directory outside `ARTIFACT_DIR`. Never pass the image value in a job
environment variable, command argument, recipe, PR comment, or shared config.
The vault is loaded only for a selected private recipe; public runs do not need it.

The reference lives on the Forge hub. Registry pull credentials belong on the
target GPU cluster. This change does not provision either secret.

## Execution and evidence

The checked submission boundary verifies the committed source and placeholder
bundle before substituting the image in memory for the secret-handled raw POST.
Public source/submission artifacts remain placeholders. Contract v3 records only
the logical binding, plus any separately checked CI placement override.

Readiness and resource API output are secret-handled. Runtime captures collect
observed image digests in memory and sanitize structured resources before writing.
Private Pod logs and image-status log blocks are omitted. Ready image IDs become
`private`, a withholding marker rather than a fabricated digest. Mismatched
serving images or heterogeneous serving digests fail capture.

Execution artifacts use a separate staging directory. Every staged file and
relative path is checked for known private identifiers before copying to the
published tree. A failed check blocks publication of that tree, including on a
failed workload. This is an additional gate, not a replacement for sanitizing
captures before serialization.

The matching Playbooks importer checks v3 against the tested recipe and overrides,
retains pre-release attribution, and omits the private digest. RC results may be
displayed under their intended GA platform without asserting a final GA image was
tested. Public replay validates sanitized configuration and measurements, not the
withheld identity.

## Local proof

Run `python -m pytest projects/inference_playbooks/tests -q`. In the Playbooks
worktree, set `FORGE_PRIVATE_COMPAT_REPO` to this worktree and run
`python -m pytest tests/test_forge_bundle_contract.py -q` for producer/importer
compatibility. These are synthetic proofs, not live cluster benchmarks.
