"""
Pydantic models for Caliper parse / visualize / KPI steps driven from ``caliper.postprocess``.
"""

from __future__ import annotations

from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class CaliperOrchestrationParseSection(BaseModel):
    """``caliper.postprocess.parse``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    no_cache: bool = False


class CaliperOrchestrationVisualizeSection(BaseModel):
    """``caliper.postprocess.visualize`` — same semantics as ``caliper visualize``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_dir: str | None = Field(
        default=None,
        description=("Directory for HTML/plots. Must be an absolute path."),
    )
    reports: str | None = Field(
        default=None,
        description="Comma-separated report ids or list of report ids (alternative to report_group).",
    )
    report_group: str | None = Field(
        default=None,
        description="Group id from visualize-groups.yaml under the artifact tree.",
    )
    visualize_config: str | None = Field(
        default=None,
        description="Path to visualize-groups YAML; default search under artifact tree.",
    )
    include_labels: list[str] = Field(default_factory=list)
    exclude_labels: list[str] = Field(default_factory=list)

    @field_validator("reports", mode="before")
    @classmethod
    def _convert_reports_list(cls, v):
        """Convert list of reports to comma-separated string."""
        if isinstance(v, list):
            return ",".join(str(item) for item in v)
        return v


class CaliperOrchestrationArtifactsToKpisSection(BaseModel):
    """Emit KPI JSON via plugin ``compute_kpis``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output: str | None = Field(
        default="kpis.json",
        description="Filename or path; relative paths resolve under the post-processing artifact dir.",
    )


class CaliperOrchestrationKpisToCsvSection(BaseModel):
    """Export KPI data to CSV format."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output: str | None = Field(
        default="kpis.csv",
        description="CSV filename or path; relative paths resolve under the post-processing artifact dir.",
    )
    include_header_comments: bool = Field(
        default=True,
        description="Whether to include descriptive header comments in the CSV file.",
    )


class CaliperOrchestrationArtifactsToAiDataSection(BaseModel):
    """Export AI evaluation payload with structured test entries and artifact files."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_dir: str = Field(
        default="ai_data",
        description="Directory name for AI evaluation export; relative paths resolve under the post-processing artifact dir.",
    )


class CaliperOrchestrationKpiSection(BaseModel):
    """``caliper.postprocess.kpi``."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    artifacts_to_kpis: CaliperOrchestrationArtifactsToKpisSection = Field(
        default_factory=CaliperOrchestrationArtifactsToKpisSection
    )
    kpis_to_csv: CaliperOrchestrationKpisToCsvSection = Field(
        default_factory=CaliperOrchestrationKpisToCsvSection
    )
    artifacts_to_ai_data: CaliperOrchestrationArtifactsToAiDataSection = Field(
        default_factory=CaliperOrchestrationArtifactsToAiDataSection
    )


class CaliperOrchestrationAnalyzeSection(BaseModel):
    """``caliper.postprocess.analyze`` — regression vs baseline KPI JSON."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    historical_kpis: str | None = Field(
        default=None,
        description="Directory containing historical KPI JSON files (relative → postprocess output dir unless absolute).",
    )
    output: str | None = Field(
        default="kpi_analyze.json",
        description="Written under post-processing artifact dir when relative.",
    )

    @model_validator(mode="after")
    def _historical_kpis_when_enabled(self) -> Self:
        if self.enabled and not (self.historical_kpis and str(self.historical_kpis).strip()):
            raise ValueError(
                "caliper.postprocess.analyze.enabled requires non-empty historical_kpis directory."
            )
        return self


class CaliperOrchestrationS3ImportSection(BaseModel):
    """Import historical data from AWS S3 for analysis."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    output_dir: str = Field(
        default="historical_data",
        description="Directory name for imported data; relative paths resolve under the post-processing artifact dir.",
    )
    include_kpis_json: bool = Field(
        default=False,
        description="Whether to download kpis.json files.",
    )
    include_kpis_csv: bool = Field(
        default=False,
        description="Whether to download kpis.csv files.",
    )
    include_ai_data: bool = Field(
        default=False,
        description="Whether to download ai_data directories.",
    )
    max_downloads: int = Field(
        default=50,
        description="Maximum number of historical entries to download.",
    )


class CaliperOrchestrationS3ExportSection(BaseModel):
    """Export postprocess artifacts to AWS S3."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    prefix: str | None = Field(
        default=None,
        description="S3 key prefix/folder path (optional).",
    )
    upload_id: str | None = Field(
        default=None,
        description="Custom upload identifier; uses timestamp (YY-MM-DD_HHMMSS) if null.",
    )
    dry_run: bool = Field(
        default=False,
        description="Show what would be uploaded without actually uploading files.",
    )
    include_csv: bool = Field(
        default=True,
        description="Whether to include CSV exports in S3 upload.",
    )
    include_kpis_json: bool = Field(
        default=True,
        description="Whether to include KPI JSON files in S3 upload.",
    )
    include_ai_data: bool = Field(
        default=True,
        description="Whether to include AI data exports in S3 upload.",
    )


class CaliperOrchestrationS3VaultSection(BaseModel):
    """Vault configuration for AWS S3 credentials."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(
        default="psap-forge-aws-s3-export",
        description="Vault name containing AWS credentials.",
    )
    aws_credentials_file: str = Field(
        default="aws.credentials",
        description="File name within vault containing AWS credentials.",
    )


class CaliperOrchestrationS3Section(BaseModel):
    """AWS S3 operations for import and export of postprocess artifacts."""

    model_config = ConfigDict(extra="forbid")

    bucket: str | None = Field(
        default=None,
        description="S3 bucket name (required when import or export is enabled).",
    )
    instance: str | None = Field(
        default=None,
        description="Instance identifier for S3 organization (required when import or export is enabled).",
    )
    directory: str | None = Field(
        default=None,
        description="Directory identifier for S3 organization (required when import or export is enabled).",
    )
    prefix: str | None = Field(
        default=None,
        description="S3 prefix for organizing uploads and imports.",
    )
    vault: CaliperOrchestrationS3VaultSection = Field(
        default_factory=CaliperOrchestrationS3VaultSection,
        description="Vault configuration for AWS credentials.",
    )
    import_: CaliperOrchestrationS3ImportSection = Field(
        default_factory=CaliperOrchestrationS3ImportSection, alias="import"
    )
    export: CaliperOrchestrationS3ExportSection = Field(
        default_factory=CaliperOrchestrationS3ExportSection
    )

    @model_validator(mode="after")
    def _required_fields_when_enabled(self) -> Self:
        s3_enabled = self.import_.enabled or self.export.enabled
        if s3_enabled:
            if not (self.bucket and str(self.bucket).strip()):
                raise ValueError(
                    "caliper.postprocess.s3.bucket is required when import or export is enabled."
                )
            if not (self.instance and str(self.instance).strip()):
                raise ValueError(
                    "caliper.postprocess.s3.instance is required when import or export is enabled."
                )
            if not (self.directory and str(self.directory).strip()):
                raise ValueError(
                    "caliper.postprocess.s3.directory is required when import or export is enabled."
                )
        return self


class CaliperOrchestrationPostprocessConfig(BaseModel):
    """``caliper.postprocess`` — parse, visualize, optional KPI + regression."""

    model_config = ConfigDict(extra="ignore")

    enabled: bool = Field(True, description="Master switch for the whole post-processing pipeline.")

    artifacts_dir: str | None = Field(
        default=None,
        description=(
            "Root of the Caliper artifact tree; when null, callers typically use "
            "ARTIFACT_BASE_DIR or override via CLI."
        ),
    )
    plugin_module: str | None = Field(
        default=None,
        description="Plugin import path; overrides manifest plugin_module when set.",
    )
    postprocess_config: str | None = Field(
        default=None,
        description="Explicit path to caliper.yaml manifest.",
    )
    parse: CaliperOrchestrationParseSection = Field(
        default_factory=CaliperOrchestrationParseSection
    )
    visualize: CaliperOrchestrationVisualizeSection = Field(
        default_factory=CaliperOrchestrationVisualizeSection
    )
    kpi: CaliperOrchestrationKpiSection = Field(default_factory=CaliperOrchestrationKpiSection)
    analyze: CaliperOrchestrationAnalyzeSection = Field(
        default_factory=CaliperOrchestrationAnalyzeSection
    )
    s3: CaliperOrchestrationS3Section = Field(default_factory=CaliperOrchestrationS3Section)

    @model_validator(mode="after")
    def _visualize_needs_selector(self) -> Self:
        if not self.visualize.enabled:
            return self
        if not (self.visualize.reports or self.visualize.report_group):
            raise ValueError(
                "caliper.postprocess.visualize.enabled requires "
                "`reports` (comma-separated) or `report_group`."
            )
        return self
