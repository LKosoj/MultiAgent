// Режим «Форма» экрана «Файлы»: чистый алгоритм (спека 5.4 плана У5/У9/У10).
// Из JSON строится плоский список полей; правка меняет только значение по пути, остальное не трогается.
// Тот же результат должна давать Tk-версия: векторы лежат в tests/fixtures/storybook_screens/golden_files_form_vectors.json.
import { ARTIFACT_LABELS } from "./artifactLabels";
import { FILES_TEXTS } from "./filesTexts";

export type JsonPath = (string | number)[];

export type FormKind =
  | "string" // короткая строка
  | "text" // длинная строка (больше 80 знаков или с переводом строки)
  | "number"
  | "bool"
  | "null" // значение не задано: пустое поле; ввод превращает его в строку
  | "lines" // массив строк: по строке на значение
  | "numlines" // массив чисел: по числу на строку
  | "group" // объект
  | "list" // массив объектов
  | "item" // элемент массива объектов
  | "raw"; // смешанное или вложенное: показывается как есть, правится только в JSON

export type FormField = {
  pointer: string;
  path: JsonPath;
  label: string;
  kind: FormKind;
  value: unknown; // у group/list/item - null
  readOnly: boolean;
  depth: number;
  // сколько полей-потомков идёт следом (group/list/item); по ним форма рисует вложенность лениво
  size: number;
};

export const LONG_STRING_LIMIT = 80;

const isPlainObject = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

export const pointerOf = (path: JsonPath): string =>
  path.map((seg) => "/" + String(seg).replace(/~/g, "~0").replace(/\//g, "~1")).join("");

// Шаблон для таблицы подписей: индексы массивов заменены на `*`
export const templateOf = (path: JsonPath): string =>
  path.map((seg) => "/" + (typeof seg === "number" ? "*" : String(seg).replace(/~/g, "~0").replace(/\//g, "~1"))).join("");

export const isLongString = (value: string): boolean => value.length > LONG_STRING_LIMIT || value.includes("\n");

// Подпись поля: таблица из схем, иначе имя ключа (у элемента массива - «Элемент N»)
export const labelFor = (artifactKey: string, path: JsonPath): string => {
  const known = ARTIFACT_LABELS[artifactKey]?.[templateOf(path)];
  const last = path[path.length - 1];
  if (typeof last === "number") return known ? `${known} ${last + 1}` : `Элемент ${last + 1}`;
  if (known) return known;
  return last === undefined ? "" : String(last);
};

const classify = (value: unknown): FormKind => {
  if (value === null) return "null";
  if (typeof value === "boolean") return "bool";
  if (typeof value === "number") return "number";
  if (typeof value === "string") return isLongString(value) ? "text" : "string";
  if (Array.isArray(value)) {
    if (value.length === 0 || value.every((v) => typeof v === "string")) return "lines";
    if (value.every((v) => typeof v === "number")) return "numlines";
    if (value.every(isPlainObject)) return "list";
    return "raw";
  }
  return isPlainObject(value) ? "group" : "raw";
};

// Список полей для формы; null - форма недоступна (корень не объект и не массив)
export const buildFields = (artifactKey: string, content: unknown): FormField[] | null => {
  const out: FormField[] = [];
  const emit = (path: JsonPath, value: unknown, depth: number) => {
    const kind = classify(value);
    const field: FormField = {
      pointer: pointerOf(path),
      path,
      label: labelFor(artifactKey, path),
      kind,
      value: kind === "group" || kind === "list" ? null : value,
      readOnly: kind === "raw",
      depth,
      size: 0,
    };
    out.push(field);
    const index = out.length - 1;
    if (kind === "group") {
      for (const [name, sub] of Object.entries(value as Record<string, unknown>)) emit([...path, name], sub, depth + 1);
    } else if (kind === "list") {
      (value as unknown[]).forEach((element, i) => {
        const itemPath = [...path, i];
        const item: FormField = {
          pointer: pointerOf(itemPath),
          path: itemPath,
          label: labelFor(artifactKey, itemPath),
          kind: "item",
          value: null,
          readOnly: false,
          depth: depth + 1,
          size: 0,
        };
        out.push(item);
        const itemIndex = out.length - 1;
        for (const [name, sub] of Object.entries(element as Record<string, unknown>)) emit([...itemPath, name], sub, depth + 2);
        item.size = out.length - itemIndex - 1;
      });
    }
    field.size = out.length - index - 1;
  };

  if (isPlainObject(content)) {
    for (const [name, sub] of Object.entries(content)) emit([name], sub, 0);
    return out;
  }
  if (Array.isArray(content)) {
    emit([], content, 0);
    // корень-список без заголовка: подпись берётся из заголовка схемы файла
    out[0].label = ARTIFACT_LABELS[artifactKey]?.[""] ?? "";
    return out;
  }
  return null;
};

// Значение в нужном месте документа (undefined, если пути нет)
export const valueAt = (content: unknown, path: JsonPath): unknown => {
  let cur: unknown = content;
  for (const seg of path) {
    if (Array.isArray(cur) && typeof seg === "number") cur = cur[seg];
    else if (isPlainObject(cur) && typeof seg === "string") cur = cur[seg];
    else return undefined;
  }
  return cur;
};

// Копия документа с новым значением по пути; остальные ключи и их порядок не меняются
export const setAt = (content: unknown, path: JsonPath, value: unknown): unknown => {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  if (Array.isArray(content) && typeof head === "number") {
    const copy = content.slice();
    copy[head] = setAt(content[head], rest, value);
    return copy;
  }
  if (isPlainObject(content) && typeof head === "string") {
    return { ...content, [head]: setAt(content[head], rest, value) };
  }
  return content;
};

const NUMBER_RE = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

// Число из текста поля; запятая считается десятичным разделителем
export const parseNumber = (text: string): number | null => {
  const normalized = text.trim().replace(",", ".");
  if (!NUMBER_RE.test(normalized)) return null;
  const n = Number(normalized);
  return Number.isFinite(n) ? n : null;
};

// Что показать в поле ввода для значения
export const fieldText = (field: Pick<FormField, "kind">, value: unknown): string => {
  if (value === null || value === undefined) return "";
  if (field.kind === "lines" || field.kind === "numlines") return Array.isArray(value) ? value.join("\n") : "";
  if (field.kind === "raw") return JSON.stringify(value, null, 2);
  return typeof value === "string" ? value : String(value);
};

export type InputResult = { content: unknown; error: string | null };

// Применить ввод пользователя к документу. Ошибка (например, не число) значение не применяет.
export const applyInput = (content: unknown, field: Pick<FormField, "kind" | "path" | "label">, input: string | boolean): InputResult => {
  const fail = (): InputResult => ({
    content,
    error: FILES_TEXTS.field_number.replace("{name}", field.label),
  });
  switch (field.kind) {
    case "bool":
      return typeof input === "boolean" ? { content: setAt(content, field.path, input), error: null } : { content, error: null };
    case "string":
    case "text":
      return typeof input === "string" ? { content: setAt(content, field.path, input), error: null } : { content, error: null };
    case "null":
      if (typeof input !== "string") return { content, error: null };
      return { content: setAt(content, field.path, input === "" ? null : input), error: null };
    case "number": {
      if (typeof input !== "string") return { content, error: null };
      const n = parseNumber(input);
      return n === null ? fail() : { content: setAt(content, field.path, n), error: null };
    }
    case "lines": {
      if (typeof input !== "string") return { content, error: null };
      const lines = input.split(/\r?\n/).filter((line) => line.trim() !== "");
      return { content: setAt(content, field.path, lines), error: null };
    }
    case "numlines": {
      if (typeof input !== "string") return { content, error: null };
      const numbers: number[] = [];
      for (const line of input.split(/\r?\n/).filter((l) => l.trim() !== "")) {
        const n = parseNumber(line);
        if (n === null) return fail();
        numbers.push(n);
      }
      return { content: setAt(content, field.path, numbers), error: null };
    }
    default:
      return { content, error: null };
  }
};

// Форма -> JSON: indent=2, кириллица как есть (так пишет сервер)
export const contentToText = (content: unknown): string => JSON.stringify(content, null, 2);

export type ParsedText = { ok: true; value: unknown } | { ok: false };

// JSON -> форма: разбор текста; при ошибке переключение блокируется, строку ошибки даёт сервер
export const textToContent = (text: string): ParsedText => {
  try {
    return { ok: true, value: JSON.parse(text) };
  } catch {
    return { ok: false };
  }
};

// --- Позиции в тексте JSON (поиск, курсор, переход от находки к строке) ---

// Строка и столбец (с единицы) для смещения
export const lineColAt = (text: string, offset: number): { line: number; column: number } => {
  const head = text.slice(0, Math.max(0, Math.min(offset, text.length)));
  const lines = head.split("\n");
  return { line: lines.length, column: lines[lines.length - 1].length + 1 };
};

// Смещение для строки и столбца (с единицы); за пределами текста - в его границы
export const offsetAt = (text: string, line: number, column: number): number => {
  const lines = text.split("\n");
  const row = Math.min(Math.max(line, 1), lines.length);
  let offset = 0;
  for (let i = 0; i < row - 1; i += 1) offset += lines[i].length + 1;
  return offset + Math.min(Math.max(column, 1) - 1, lines[row - 1].length);
};

const WS = /\s/;

const skipWs = (t: string, i: number): number => {
  let k = i;
  while (k < t.length && WS.test(t[k])) k += 1;
  return k;
};

const skipString = (t: string, i: number): number => {
  let k = i + 1;
  while (k < t.length && t[k] !== '"') k += t[k] === "\\" ? 2 : 1;
  return k + 1;
};

// Конец значения, начинающегося в позиции i (текст считается корректным JSON)
const skipValue = (t: string, i: number): number => {
  const c = t[i];
  if (c === '"') return skipString(t, i);
  if (c === "{" || c === "[") {
    let depth = 0;
    let k = i;
    while (k < t.length) {
      const ch = t[k];
      if (ch === '"') {
        k = skipString(t, k);
        continue;
      }
      if (ch === "{" || ch === "[") depth += 1;
      else if (ch === "}" || ch === "]") {
        depth -= 1;
        if (depth === 0) return k + 1;
      }
      k += 1;
    }
    return k;
  }
  let k = i;
  while (k < t.length && !",}]".includes(t[k]) && !WS.test(t[k])) k += 1;
  return k;
};

// Смещение значения по пути в тексте JSON; null - пути нет или текст не разобрался
export const jsonOffsetOf = (text: string, path: JsonPath): number | null => {
  let i = skipWs(text, 0);
  for (const seg of path) {
    const open = text[i];
    if (open === "{" && typeof seg === "string") {
      let k = skipWs(text, i + 1);
      let found = -1;
      while (k < text.length && text[k] === '"') {
        const keyEnd = skipString(text, k);
        let name: string;
        try {
          name = JSON.parse(text.slice(k, keyEnd)) as string;
        } catch {
          return null;
        }
        const colon = skipWs(text, keyEnd);
        const valueStart = skipWs(text, colon + 1);
        if (name === seg) {
          found = valueStart;
          break;
        }
        k = skipWs(text, skipValue(text, valueStart));
        if (text[k] === ",") k = skipWs(text, k + 1);
      }
      if (found < 0) return null;
      i = found;
    } else if (open === "[" && typeof seg === "number") {
      let k = skipWs(text, i + 1);
      let index = 0;
      let found = -1;
      while (k < text.length && text[k] !== "]") {
        if (index === seg) {
          found = k;
          break;
        }
        k = skipWs(text, skipValue(text, k));
        if (text[k] === ",") k = skipWs(text, k + 1);
        index += 1;
      }
      if (found < 0) return null;
      i = found;
    } else {
      return null;
    }
  }
  return i;
};
