# Changelog Structure Guide

This document describes the standard structure and conventions for changelog files in FORGE projects.

## File Types and Naming

### Project Changelogs
- **Single changelog**: `projects/{project}/CHANGELOG.md` - For projects with mixed orchestration/toolbox changes
- **Orchestration changelog**: `projects/{project}/CHANGELOG-orchestration.md` - For orchestration-specific changes
- **Toolbox changelog**: `projects/{project}/CHANGELOG-toolbox.md` - For toolbox-specific changes
- **Component changelog**: `projects/{project}/CHANGELOG-{component}.md` - For specific components (e.g. `CHANGELOG-DSL.md`, `CHANGELOG-notifications.md`)

### When to Use Which Type
- **Orchestration changelog**: Changes to CI phases, configuration, test organization, deployment orchestration
- **Toolbox changelog**: Changes to specific toolbox scripts and their functionality
- **Single changelog**: For projects with predominantly one type of change or mixed small changes
- **Component changelog**: For core components that span multiple projects

## Structure Hierarchy

### Orchestration Changelogs
Standard changelog format focused on orchestration workflow changes:

```markdown
# {Project} Orchestration Changelog

## YYYY-MM-DD - Brief Summary of Changes

### Feature/Change Category
- **Change Description**: Detailed explanation
  - **Sub-detail**: Additional context
  - **Impact**: What this enables or improves

### Files Modified
- `projects/{project}/orchestration/file.py` - Description of changes

### Benefits
- **Benefit 1**: Explanation of value provided
- **Benefit 2**: Additional advantages
```

### Toolbox Changelogs
Structured hierarchy organized by toolbox script:

```markdown
# {Project} Toolbox Changelog

## YYYY-MM-DD - Brief Summary of Changes

### toolbox_script_name

#### Feature/Change Category
- **Change Description**: Detailed explanation of what changed
  - **Technical Detail**: Implementation specifics
  - **Behavior Change**: How usage is affected

#### Files Added
- `projects/{project}/toolbox/{script}/new_file.py` - Description (NEW)

#### Files Modified  
- `projects/{project}/toolbox/{script}/main.py` - Description of changes

#### Benefits
- **Benefit 1**: Specific advantage provided by these changes
- **Benefit 2**: Additional value delivered

#### Optional Sections
- **Agent Integration Features**: For toolboxes that add AI agent support
- **Artifact Structure**: For toolboxes that create specific output structures
```

### Component Changelogs
For core framework components:

```markdown
# {Component} Framework Changelog

## YYYY-MM-DD - Brief Summary of Changes

### New Features
- **Feature Name**: Description and usage
  - **Behavior**: How it works
  - **Usage**: How to use it

### Changed
- **Enhancement**: What was improved and why

### Files Modified
- `projects/core/{component}/file.py` - Description

### Benefits
- **Value 1**: What this enables
- **Value 2**: Additional advantages
```

## Required Structure Elements

### Toolbox Changelog Requirements

1. **Level 1**: Main title with "Toolbox" designation
2. **Level 2**: Date and summary of all changes in the release
3. **Level 3**: Individual toolbox script names (exact directory names)
4. **Level 4**: Change categories (e.g., "New Features", "Reliability Improvements", "Agent Integration")
5. **Bullets**: Detailed descriptions with bold highlights for key terms

### Content Guidelines

#### Change Categories (Level 4)
Common categories for toolbox changes:
- **New Toolbox Script**: For completely new toolbox functionality
- **Reliability Improvements**: Bug fixes, error handling, timeout adjustments
- **Agent Integration**: AI agent failure analysis, AGENT.md generation
- **Performance Enhancements**: Speed improvements, optimization
- **Logging Improvements**: Better visibility, status reporting
- **Configuration Changes**: New options, behavior modifications
- **Artifact Collection**: Enhanced debugging information capture

#### Required Sections
- **Files Added**: New files with "(NEW)" designation
- **Files Modified**: Changed files with brief description
- **Benefits**: Value provided by the changes

#### Optional Sections
- **Agent Integration Features**: Include artifact directory structure
- **Configuration Examples**: For significant config changes
- **Migration Notes**: For breaking changes

## Writing Style Guidelines

### Formatting Conventions
- **Bold key terms**: Use bold for important concepts, features, and benefits
- **Consistent terminology**: Use the same terms throughout (e.g., "ClusterPolicy" not "cluster policy")
- **Action-oriented**: Use active voice and action verbs
- **Specific impacts**: Explain what changes mean for users

### Content Best Practices
- **Lead with value**: Start descriptions with the benefit or purpose
- **Technical accuracy**: Include specific file paths, configuration keys, timeouts
- **Context**: Explain why changes were made, not just what changed
- **User impact**: Describe how changes affect the user experience

### File Path References
- Use full relative paths from repository root: `projects/{project}/toolbox/{script}/main.py`
- Mark new files with "(NEW)" designation
- Group by Added vs Modified for clarity

## Examples

### Good Toolbox Changelog Entry
```markdown
### bootstrap_gpu_clusterpolicy

#### Reliability Improvements
- **Enhanced Error Handling**: Improved ClusterPolicy readiness detection and error reporting
  - **Better Status Tracking**: More robust monitoring of ClusterPolicy state transitions  
  - **Improved Logging**: Enhanced progress reporting throughout bootstrap process

#### Agent Integration
- **Automated Failure Analysis**: Added comprehensive failure analysis for ClusterPolicy bootstrap failures
  - **AGENT.md Generation**: Creates detailed failure context for AI agent review
  - **Diagnostic Capture**: Comprehensive artifact collection for troubleshooting

#### Files Modified
- `projects/gpu_operator/toolbox/bootstrap_gpu_clusterpolicy/main.py` - Enhanced reliability and agent integration
- `projects/gpu_operator/toolbox/bootstrap_gpu_clusterpolicy/on_failure_helpers.py` - Agent analysis support (NEW)
```

### Good Orchestration Changelog Entry
```markdown
## 2026-06-26 - Orchestration & Cleanup Improvements

### Preflight Phase
- **New Orchestration Phase**: Added pre-execution validation phase
  - **Purpose**: Validate environment and prerequisites before resource provisioning
  - **Integration**: Integrated into CI pipeline for early failure detection

### Files Modified
- `projects/llm_d/orchestration/ci.py` - Preflight phase integration
- `projects/llm_d/orchestration/preflight_phase.py` - New preflight validation phase (NEW)
```

## Validation Checklist

When creating or updating changelog entries:

- [ ] Correct file type (orchestration vs toolbox vs component)
- [ ] Proper hierarchy levels (1→2→3→4→bullets)
- [ ] Toolbox names match actual directory names
- [ ] All file paths are accurate and complete
- [ ] New files marked with "(NEW)"
- [ ] Benefits section explains user value
- [ ] Change descriptions are specific and actionable
- [ ] Formatting follows bold key terms convention
- [ ] Technical details are accurate (timeouts, configuration keys, etc.)
- [ ] Content matches the type of changes (no toolbox content in orchestration changelog)

## Migration Notes

### Existing Changelogs
When updating existing changelog files to match this structure:

1. **Identify change types**: Separate orchestration vs toolbox changes
2. **Split if needed**: Create separate files if mixed content exists
3. **Restructure hierarchy**: Apply correct level structure for toolbox files
4. **Preserve content**: Keep all existing information, just reorganize structure
5. **Update file paths**: Ensure all references are accurate and complete

### Backward Compatibility
- Existing changelog URLs remain valid
- Content is preserved, only structure changes
- Links to specific sections may need updating due to heading level changes