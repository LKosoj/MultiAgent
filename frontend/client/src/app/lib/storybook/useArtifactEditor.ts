"use client";

import { useEffect, useMemo, useSyncExternalStore } from "react";

import { createArtifactEditor, type ArtifactEditor, type EditorState } from "./artifactEditor";
import type { StorybookClient } from "./client";

export type ArtifactEditorApi = { editor: ArtifactEditor; state: EditorState };

type Options = {
  client: StorybookClient;
  projectId: string;
  say: (text: string, type?: "error" | "success" | "info") => void;
  onSaved: () => void;
};

// Тонкая обёртка: редактор живёт, пока открыт экран; при уходе правки уходят в черновик
export const useArtifactEditor = (o: Options): ArtifactEditorApi => {
  const { client, projectId, say, onSaved } = o;
  // say и onSaved меняются при каждом рендере оболочки: редактор берёт их через ссылку
  const hooks = useMemo(() => ({ say, onSaved }), []); // eslint-disable-line react-hooks/exhaustive-deps
  hooks.say = say;
  hooks.onSaved = onSaved;
  const editor = useMemo(
    () => createArtifactEditor({ client, projectId, say: (t, k) => hooks.say(t, k), onSaved: () => hooks.onSaved() }),
    [client, projectId, hooks],
  );
  useEffect(() => {
    editor.activate();
    return () => editor.dispose();
  }, [editor]);
  const state = useSyncExternalStore(editor.subscribe, editor.getState, editor.getState);

  // закрытие вкладки с несохранёнными правками
  useEffect(() => {
    if (!state.dirty) return undefined;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [state.dirty]);

  return { editor, state };
};
