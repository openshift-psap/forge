# Cluster Toolbox Changelog

## 2026-06-26 - Operator Management Enhancements

### bootstrap_lws_operator

#### New Toolbox Script
- **Purpose**: Configures and bootstraps LeaderWorkerSetOperator after subscription
- **Template**: Includes LeaderWorkerSetOperator YAML template with proper configuration
- **Integration**: Enables LeaderWorkerSet workload CRD installation
- **Workflow**: Handles operator configuration post-subscription to activate workload CRDs

#### Files Added
- `projects/cluster/toolbox/bootstrap_lws_operator/main.py` - LWS operator bootstrap logic (NEW)
- `projects/cluster/toolbox/bootstrap_lws_operator/templates/leaderworkersetoperator.yaml.j2` - Operator template (NEW)

#### Benefits
- **Complete LWS Lifecycle**: Full LeaderWorkerSet operator management from subscription to workload CRDs
- **Template-Based Configuration**: Consistent operator configuration through Jinja2 templates
- **CRD Activation**: Proper activation of LeaderWorkerSet workload capabilities

### cluster_deploy_operator

#### Reliability Improvements  
- **CSV Handling**: Enhanced ClusterServiceVersion (CSV) detection and management
  - **Timeout Extension**: Increased CSV wait time to 5 minutes for better reliability
  - **Missing CSV Protection**: Added graceful handling when CSV is not found
  - **Existing CSV Logic**: Improved detection and handling of pre-existing CSV installations

#### InstallPlan Management
- **Manual InstallPlan Support**: Added logic to handle manual approval InstallPlans
  - **Non-blocking**: Ensures automation doesn't get stuck on manual approval requirements  
  - **Smart Detection**: Identifies when manual intervention is required vs. automatic approval

#### Agent Integration
- **Failure Analysis**: Added automated failure analysis for InstallPlan wait failures
  - **AGENT.md Generation**: Creates structured failure context for AI agent review
  - **Troubleshooting Guide**: Provides detailed analysis instructions and artifact references
  - **Diagnostic Capture**: Includes relevant Kubernetes resource states and events

#### Files Added
- `projects/cluster/toolbox/cluster_deploy_operator/on_failure_helpers.py` - Agent failure analysis (NEW)

#### Files Modified
- `projects/cluster/toolbox/cluster_deploy_operator/main.py` - Enhanced CSV handling, InstallPlan logic, and failure analysis

#### Benefits
- **Improved Reliability**: Better handling of operator deployment edge cases
- **Enhanced Debugging**: AI-assisted failure analysis for complex operator issues
- **Manual Approval Support**: Handles both automatic and manual InstallPlan scenarios
- **Robust CSV Management**: Graceful handling of various CSV states and timing issues

#### Agent Integration Features
```
{artifact_dir}/
├── AGENT.md                                    # Failure analysis context
├── artifacts/
│   ├── subscription.yaml                      # Operator subscription state
│   ├── installplan.yaml                       # InstallPlan details  
│   ├── clusterserviceversion.yaml            # CSV status
│   └── events.yaml                           # Relevant cluster events
└── task.log
```
