import React from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { describe, expect, it } from "vitest";

import { WorkflowsSection } from "../WorkflowsSection";

const render = (sourcePath = "", workflow = "book_summary") => renderToStaticMarkup(
  React.createElement(WorkflowsSection, {
    isBusy: false,
    workflowTab: "run",
    selectedWorkflow: { name: workflow },
    workflowInputs: { source_path: "", review_plan: false },
    workflowParams: { source_path: sourcePath, review_plan: false },
    workflowOptions: { useEnhanced: true, enableTelemetry: false },
    workflowRuns: [], workflowRunHistory: [], currentWorkflowRunLogs: [],
    workflowResults: {}, currentWorkflowRunId: null,
  } as unknown as React.ComponentProps<typeof WorkflowsSection>),
);

describe("загрузка книги в форме запуска", () => {
  it("предлагает PDF/EPUB и не запускает пайплайн до загрузки", () => {
    const html = render();
    expect(html).toContain("Файл книги");
    expect(html).toContain('type="file"');
    expect(html).toContain('accept=".pdf,.epub,application/pdf,application/epub+zip"');
    expect(html).toMatch(/<button[^>]*disabled=""[^>]*>Запустить<\/button>/);
    expect(html).toMatch(/<input type="checkbox"\/><span>review_plan<\/span>/);
  });

  it("показывает имя загруженной книги и разрешает запуск", () => {
    const html = render("/books/id/input/Книга.pdf");
    expect(html).toContain("Книга загружена: Книга.pdf");
    expect(html).not.toMatch(/<button[^>]*disabled=""[^>]*>Запустить<\/button>/);
    expect(html).not.toContain("/books/id/input");
  });

  it("сохраняет обычные поля других пайплайнов", () => {
    const html = render("", "another_workflow");
    expect(html).not.toContain('type="file"');
    expect(html).toContain("source_path");
  });
});
