"use client";

import React, { useEffect, useRef, useState } from "react";

import { lineColAt } from "../../../../lib/storybook/artifactForm";
import { FILES_TEXTS, filesFmt } from "../../../../lib/storybook/filesTexts";

type Props = {
  text: string;
  readOnly: boolean;
  onChange: (text: string) => void;
  // смещение, куда надо поставить курсор (переход от находки); n меняется при каждом переходе
  jump: { offset: number; n: number } | null;
  // подпись для скринридера: на экране может быть два редактора
  label?: string;
};

// Редактор JSON: «Строка N, столбец M», поиск (Ctrl+F внутри редактора)
export function JsonEditor({ text, readOnly, onChange, jump, label = "JSON" }: Props) {
  const ref = useRef<HTMLTextAreaElement>(null);
  const [pos, setPos] = useState({ line: 1, column: 1 });
  const [searchOpen, setSearchOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [notFound, setNotFound] = useState(false);
  const searchRef = useRef<HTMLInputElement>(null);

  const syncPos = () => {
    const el = ref.current;
    if (el) setPos(lineColAt(el.value, el.selectionStart));
  };

  useEffect(() => {
    const el = ref.current;
    if (!el || !jump) return;
    el.focus();
    el.setSelectionRange(jump.offset, jump.offset);
    // прокрутка к строке: высота строки берётся из стиля
    const line = lineColAt(el.value, jump.offset).line;
    const lh = parseFloat(getComputedStyle(el).lineHeight) || 18;
    el.scrollTop = Math.max(0, (line - 3) * lh);
    syncPos();
  }, [jump]);

  const find = (from: number) => {
    const el = ref.current;
    if (!el || query === "") return;
    const hay = el.value.toLowerCase();
    const needle = query.toLowerCase();
    let at = hay.indexOf(needle, from);
    if (at < 0) at = hay.indexOf(needle);
    if (at < 0) {
      setNotFound(true);
      return;
    }
    setNotFound(false);
    el.focus();
    el.setSelectionRange(at, at + needle.length);
    syncPos();
  };

  return (
    <div className="sb-files-json">
      {searchOpen ? (
        <div className="sb-files-search" role="search">
          <input
            ref={searchRef}
            type="search"
            aria-label={FILES_TEXTS.json_search}
            placeholder={FILES_TEXTS.json_search}
            value={query}
            onChange={(e) => {
              setQuery(e.target.value);
              setNotFound(false);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                find((ref.current?.selectionEnd ?? 0));
              } else if (e.key === "Escape") {
                e.stopPropagation();
                setSearchOpen(false);
                ref.current?.focus();
              }
            }}
          />
          <button type="button" className="button secondary" onClick={() => find(ref.current?.selectionEnd ?? 0)}>
            {FILES_TEXTS.json_search_next}
          </button>
          {notFound ? <span className="sb-muted" role="status">{FILES_TEXTS.json_search_none}</span> : null}
        </div>
      ) : null}
      <textarea
        ref={ref}
        className="sb-files-json-text"
        aria-label={label}
        spellCheck={false}
        readOnly={readOnly}
        value={text}
        onChange={(e) => {
          onChange(e.target.value);
          setPos(lineColAt(e.target.value, e.target.selectionStart));
        }}
        onKeyUp={syncPos}
        onClick={syncPos}
        onSelect={syncPos}
        onKeyDown={(e) => {
          if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "f") {
            e.preventDefault();
            setSearchOpen(true);
            setTimeout(() => searchRef.current?.focus(), 0);
          }
        }}
      />
      <div className="sb-files-pos sb-muted" aria-live="off">
        {filesFmt("json_position", { line: pos.line, column: pos.column })}
      </div>
    </div>
  );
}
