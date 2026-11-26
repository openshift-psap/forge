# IBM Cloud Cluster Toolbox Changelog

## ibmcloud

- Save only the installer metadata resource-group name to a public recovery
  artifact without printing it or exposing the full metadata file.

- Link generated cluster names and saved inputs/status to the originating FournosJob.
- Name the release command helper `run_step_command` and the install-config path explicitly.

- Define a public Pydantic settings-file contract and validate create/destroy
  vault-directory requirements at the orchestration and toolbox boundaries.

- Group control-plane, worker, and release settings into nested configuration.

- Keep IBM provisioning in the `ibmcloud_cluster` project with separate create
  and destroy toolbox commands and shared release helpers.
- Use the optional `env.FORGE_SHARED_DIR` workspace and a vault-name constant.
- Add a two-task IBM Cloud create/destroy pipeline, with destroy in finally.
- Wrap five pinned release steps, adapting their CI inputs and working paths.
- Use separate create and destroy DSL entrypoints with named tasks, retaining
  raw upstream output internally and safe task metadata in public artifacts.
- Make the CI entrypoint executable, notify on teardown failures, and resume
  interrupted private-file cleanup after confirmed cloud deletion.
- Preserve multiline pull-secret JSON through a private configuration copy and
  retain string resource-group names during YAML generation.
- Default to au-syd-1, three bxf-8x32 control planes and three bxf-16x64 workers.
- Resolve credentials through the existing Forge/Fournos vault mechanism.
- Reuse Fournos's existing PVC with task workspace subpaths for public artifacts
  and internal state.
- Add offline coverage for the DSL lifecycle, configuration overrides, release
  scripts, private output handling, pipeline workspace mappings, and artifact
  collection while private state remains present after success or failures.
