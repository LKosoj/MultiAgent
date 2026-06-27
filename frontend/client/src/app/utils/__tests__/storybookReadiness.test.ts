import { describe, expect, it } from "vitest";
import {
  STORYBOOK_WORKFLOW_NAME,
  buildStorybookProjectPayload,
  buildStorybookReadinessPayload,
  extractStorybookReadiness,
  isStorybookWorkflowName,
  summarizeStorybookReadiness,
  summarizeStorybookWorkflowActions,
} from "../storybookReadiness";

describe("storybook readiness helpers", () => {
  it("builds a top-level readiness payload without requiring task", () => {
    const payload = buildStorybookReadinessPayload(
      {
        task: "yaml prompt",
        project_id: "storybook_project",
        language: "ru",
        generate_screenplay: true,
        generate_music: true,
      },
      {
        task: "",
        project_id: "video-demo",
        language: "en",
        generate_screenplay: "false",
        generate_music: "true",
      },
    );

    expect(STORYBOOK_WORKFLOW_NAME).toBe("storybook_pipeline");
    expect(isStorybookWorkflowName("storybook_pipeline")).toBe(true);
    expect(isStorybookWorkflowName("other_pipeline")).toBe(false);
    expect(payload).toEqual({
      project_id: "video-demo",
      language: "en",
      enable: false,
      generate_music: true,
    });
    expect(payload).not.toHaveProperty("task");
    expect(payload).not.toHaveProperty("parameters");
    expect(buildStorybookProjectPayload({ project_id: "default" }, { project_id: "video-demo" })).toEqual({
      project_id: "video-demo",
    });
  });

  it("unwraps readiness responses", () => {
    const readiness = { status: "success", project_id: "demo" };

    expect(extractStorybookReadiness({ readiness })).toBe(readiness);
    expect(extractStorybookReadiness({ result: { readiness } })).toBe(readiness);
    expect(extractStorybookReadiness(readiness)).toBe(readiness);
    expect(extractStorybookReadiness(null)).toBeNull();
  });

  it("summarizes readiness into stable UI rows", () => {
    const summary = summarizeStorybookReadiness({
      status: "success",
      ready: false,
      project_id: "demo",
      generation_enabled: true,
      capabilities: { image: true, video: true, audio: true, music: false, render: true },
      video: { provider: "aitunnel", expected_clip_count: 2, shots_exists: true },
      music: {
        enabled: true,
        provider: "suno",
        configured: false,
        model: "V4_5",
        callback_configured: false,
        status: "not_generated",
        music_exists: false,
      },
      render: { ffmpeg_path: "/usr/bin/ffmpeg", ffprobe_path: "/usr/bin/ffprobe", configured: true },
      artifacts: {
        music_manifest: { status: "missing" },
        music: { status: "missing" },
        final_video: { status: "present" },
        final_review: { status: "passed" },
      },
      final_review: { exists: true, passed: true, failed_checks: [] },
      blocking_reasons: [],
      warnings: ["music_provider_unavailable"],
      errors: [],
    });

    expect(summary).toMatchObject({
      status: "success",
      projectId: "demo",
      generationEnabled: true,
      ready: false,
      severity: "warning",
      blockers: [],
      warnings: ["music_provider_unavailable"],
      errors: [],
    });
    expect(summary.rows.map((row) => row.key)).toEqual([
      "provider",
      "video",
      "music",
      "render",
      "artifacts",
      "final_review",
    ]);
    expect(summary.rows.find((row) => row.key === "music")).toMatchObject({
      label: "Music provider",
      level: "warning",
    });
    expect(summary.rows.find((row) => row.key === "final_review")).toMatchObject({
      level: "success",
      value: "passed=yes, failed_checks=none",
    });
  });

  it("handles missing nested readiness objects", () => {
    const summary = summarizeStorybookReadiness({ status: "success" });

    expect(summary.status).toBe("success");
    expect(summary.projectId).toBe("");
    expect(summary.blockers).toEqual([]);
    expect(summary.warnings).toEqual([]);
    expect(summary.errors).toEqual([]);
    expect(summary.rows.map((row) => row.key)).toEqual(["provider", "video", "music", "render"]);
    expect(summary.rows.find((row) => row.key === "video")?.value).toContain("provider=unknown");
  });

  it("keeps header severity actionable when backend ready is true with warnings or errors", () => {
    expect(
      summarizeStorybookReadiness({
        status: "success",
        ready: true,
        blocking_reasons: [],
        warnings: ["shots_json_missing_before_run"],
        errors: [],
      }).severity,
    ).toBe("warning");

    const failed = summarizeStorybookReadiness({
      status: "success",
      ready: true,
      blocking_reasons: [],
      warnings: [],
      errors: ["final_review_failed"],
    });

    expect(failed.ready).toBe(true);
    expect(failed.severity).toBe("error");
    expect(failed.rows.find((row) => row.key === "provider")).toMatchObject({
      level: "error",
    });
  });

  it("prioritizes failed music status over configured music provider", () => {
    const failedByStatus = summarizeStorybookReadiness({
      music: {
        enabled: true,
        configured: true,
        music_exists: true,
        status: "failed",
      },
      errors: [],
    });
    const failedByError = summarizeStorybookReadiness({
      music: {
        enabled: true,
        configured: true,
        music_exists: true,
        status: "success",
      },
      errors: ["music_generation_failed"],
    });

    expect(failedByStatus.rows.find((row) => row.key === "music")).toMatchObject({
      level: "error",
    });
    expect(failedByError.rows.find((row) => row.key === "music")).toMatchObject({
      level: "error",
    });
  });

  it("summarizes shared workflow actions with per-surface availability", () => {
    const summary = summarizeStorybookWorkflowActions({
      workflow_actions: {
        workflow_name: "storybook_pipeline",
        project_id: "demo",
        actions: [
          {
            id: "validate_project",
            label: "Validate project",
            category: "preflight",
            status: "available",
            service_action: "workflows.storybook_validate",
            surfaces: { storybook_manager: "available", react: "available", streamlit: "available" },
          },
          {
            id: "regenerate_video",
            label: "Regenerate one video shot",
            category: "selective_regeneration",
            status: "not_implemented",
            service_action: null,
            surfaces: { storybook_manager: "not_implemented", react: "not_implemented", streamlit: "not_implemented" },
          },
        ],
      },
    });

    expect(summary).toMatchObject({
      workflowName: "storybook_pipeline",
      projectId: "demo",
    });
    expect(summary?.actions.map((action) => action.id)).toEqual(["validate_project", "regenerate_video"]);
    expect(summary?.rows.find((row) => row.key === "validate_project")).toMatchObject({
      level: "success",
      value: "available; manager=available, react=available, streamlit=available",
    });
    expect(summary?.rows.find((row) => row.key === "regenerate_video")).toMatchObject({
      level: "error",
    });
  });
});
