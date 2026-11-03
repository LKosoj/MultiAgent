"use client";

import React, { useCallback, useEffect, useRef, useState } from "react";

import { validateAllFiles } from "../../../../lib/storybook/artifactEditor";
import type { BackgroundQueue } from "../../../../lib/storybook/backgroundQueue";
import type { StorybookClient } from "../../../../lib/storybook/client";
import { FILES_TEXTS } from "../../../../lib/storybook/filesTexts";
import { summarizeValidateAll } from "../../../../lib/storybook/filesView";
import { IMAGE_TEXTS } from "../../../../lib/storybook/imageToolsTexts";
import { isRunBusy } from "../../../../lib/storybook/shotsView";
import { useArtifactEditor } from "../../../../lib/storybook/useArtifactEditor";
import { useArtifacts } from "../../../../lib/storybook/useArtifacts";
import { useIllustrations } from "../../../../lib/storybook/useIllustrations";
import type { EditJobs } from "../../../../lib/storybook/imageEditJob";
import { useImageEdit } from "../../../../lib/storybook/useImageEdit";
import { useShotMedia } from "../../../../lib/storybook/useShotMedia";
import { ImageEditDialog } from "../imagetools/ImageEditDialog";
import { IllustrationsGallery } from "../imagetools/IllustrationsGallery";
import { FilesTab } from "./FilesTab";
import type { ValidateAllState } from "./FilesDialogs";

type Props = {
  // ожидание правки картинки ИИ живёт в разделе и переживает смену вкладок
  editJobs: EditJobs;
  client: StorybookClient;
  background: BackgroundQueue;
  runUser: <T>(task: () => Promise<T>) => Promise<T>;
  projectId: string;
  runState: string | undefined;
  active: boolean;
  say: (text: string, type?: "error" | "success" | "info") => void;
};

// Контейнер экрана «Файлы»: хуки ходят в сеть, компонент только рисует
export function StorybookFilesTab({ editJobs, client, background, runUser, projectId, runState, active, say }: Props) {
  const [mode, setMode] = useState<"docs" | "images">("docs");
  const list = useArtifacts(client, projectId, active);
  const media = useShotMedia({ client, background, projectId, enabled: active && mode === "images" });
  const illustrations = useIllustrations({ client, runUser, projectId, enabled: active && mode === "images" });
  const { reload: reloadIllustrations } = illustrations;
  const edit = useImageEdit({
    client,
    runUser,
    projectId,
    say,
    jobs: editJobs,
    onDone: () => {
      media.bust();
      reloadIllustrations();
    },
  });
  const { refreshQuiet } = list;
  const ed = useArtifactEditor({ client, projectId, say, onSaved: refreshQuiet });

  const [state, setState] = useState<ValidateAllState>({ phase: "idle" });
  const cancelled = useRef(false);
  useEffect(() => {
    cancelled.current = false;
    return () => {
      cancelled.current = true;
    };
  }, []);

  const run = useCallback(() => {
    setState({ phase: "running" });
    void validateAllFiles(client, projectId, list.items, () => cancelled.current).then((entries) => {
      if (cancelled.current) return;
      const summary = summarizeValidateAll(entries);
      setState({ phase: "done", entries, summary });
      if (summary.passed && summary.checked > 0) say(FILES_TEXTS.toast_valid, "success");
    });
  }, [client, projectId, list.items, say]);
  const close = useCallback(() => setState({ phase: "idle" }), []);

  const modes = [
    { id: "docs", label: IMAGE_TEXTS.mode_docs },
    { id: "images", label: IMAGE_TEXTS.mode_images },
  ] as const;
  const editTarget = edit.target;
  const editedItem = editTarget ? illustrations.items.find((i) => i.path === editTarget.path) : undefined;
  const editedRev = editedItem ? String(editedItem.modified_ts) : null;
  return (
    <>
      <div className="sb-imagetools-modes" role="group">
        {modes.map((m) => (
          <button key={m.id} type="button" className="button secondary" aria-pressed={mode === m.id} onClick={() => setMode(m.id)}>
            {m.label}
          </button>
        ))}
      </div>
      {mode === "docs" ? (
        <FilesTab list={list} ed={ed} validateAll={{ state, run, close }} say={say} />
      ) : (
        <IllustrationsGallery list={illustrations} edit={edit} media={media} runBusy={isRunBusy(runState)} />
      )}
      {edit.open && editTarget ? (
        <ImageEditDialog
          api={edit}
          imageUrl={media.media(editTarget.path, editedRev)}
        />
      ) : null}
    </>
  );
}
