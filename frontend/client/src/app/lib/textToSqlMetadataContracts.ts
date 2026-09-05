import { hasOwnField, isRecord } from "./textToSqlContracts";

// Contract mirrors backend `text_to_sql.metadata.*` actions
// (docs/plans/2026-09-05-text2sql-metadata-editor.md, §1). Guard functions
// follow the same "no `any` leaks the boundary" style as textToSqlContracts.ts:
// every field is checked before it is trusted, unknown extra fields are
// ignored rather than rejected (forward-compatible with backend additions).

export type TextToSqlColumnMetadata = {
  type?: string;
  description: string;
  description_source: "file" | "none";
  examples: unknown[];
  examples_source: "file" | "none";
};

export type TextToSqlTableMetadata = {
  description: string;
  description_source: "file" | "none";
  columns: Record<string, TextToSqlColumnMetadata>;
};

export type TextToSqlGlossaryEntry = {
  term: string;
  synonyms: string[];
  table: string;
  column: string | null;
  kind: "dimension" | "measure" | "filter_value" | "entity" | null;
  note: string | null;
};

export type TextToSqlSemanticFact = {
  fact_key: string;
  subject: "table" | "column";
  table_fqn: string;
  column: string | null;
  fact_kind: "description" | "example" | "glossary_term";
  value: unknown;
  status: "approved" | "rejected";
};

export type TextToSqlMetadataView = {
  connection_ref: string;
  schema_digest: string | null;
  editable_file_enabled: boolean | null;
  tables: Record<string, TextToSqlTableMetadata>;
  glossary: { digest: string; profile_exists: boolean; entries: TextToSqlGlossaryEntry[] };
  facts: TextToSqlSemanticFact[];
};

const DESCRIPTION_SOURCES = new Set(["file", "none"]);
const GLOSSARY_KINDS = new Set(["dimension", "measure", "filter_value", "entity"]);
const FACT_SUBJECTS = new Set(["table", "column"]);
const FACT_KINDS = new Set(["description", "example", "glossary_term"]);
const FACT_STATUSES = new Set(["approved", "rejected"]);

function isDescriptionSource(value: unknown): value is "file" | "none" {
  return typeof value === "string" && DESCRIPTION_SOURCES.has(value);
}

function parseColumnMetadata(value: unknown): TextToSqlColumnMetadata {
  if (
    !isRecord(value)
    || typeof value.description !== "string"
    || !isDescriptionSource(value.description_source)
    || !Array.isArray(value.examples)
    || !isDescriptionSource(value.examples_source)
  ) {
    throw new Error("Invalid Text-to-SQL column metadata shape");
  }
  return {
    ...(typeof value.type === "string" ? { type: value.type } : {}),
    description: value.description,
    description_source: value.description_source,
    examples: value.examples,
    examples_source: value.examples_source,
  };
}

function parseTableMetadata(value: unknown): TextToSqlTableMetadata {
  if (
    !isRecord(value)
    || typeof value.description !== "string"
    || !isDescriptionSource(value.description_source)
    || !isRecord(value.columns)
  ) {
    throw new Error("Invalid Text-to-SQL table metadata shape");
  }
  const columns: Record<string, TextToSqlColumnMetadata> = {};
  for (const [columnName, column] of Object.entries(value.columns)) {
    columns[columnName] = parseColumnMetadata(column);
  }
  return {
    description: value.description,
    description_source: value.description_source,
    columns,
  };
}

function parseGlossaryEntry(value: unknown): TextToSqlGlossaryEntry {
  if (
    !isRecord(value)
    || typeof value.term !== "string"
    || !Array.isArray(value.synonyms)
    || !value.synonyms.every((synonym) => typeof synonym === "string")
    || typeof value.table !== "string"
    || (value.column !== null && typeof value.column !== "string")
    || (value.kind !== null && !(typeof value.kind === "string" && GLOSSARY_KINDS.has(value.kind)))
    || (value.note !== null && typeof value.note !== "string")
  ) {
    throw new Error("Invalid Text-to-SQL glossary entry shape");
  }
  return {
    term: value.term,
    synonyms: value.synonyms as string[],
    table: value.table,
    column: value.column as string | null,
    kind: value.kind as TextToSqlGlossaryEntry["kind"],
    note: value.note as string | null,
  };
}

export function parseTextToSqlGlossaryEntries(payload: unknown): TextToSqlGlossaryEntry[] {
  if (!Array.isArray(payload)) {
    throw new Error("Invalid Text-to-SQL glossary entries shape");
  }
  return payload.map(parseGlossaryEntry);
}

function parseSemanticFact(value: unknown): TextToSqlSemanticFact {
  if (
    !isRecord(value)
    || typeof value.fact_key !== "string"
    || !value.fact_key
    || !(typeof value.subject === "string" && FACT_SUBJECTS.has(value.subject))
    || typeof value.table_fqn !== "string"
    || (value.column !== null && typeof value.column !== "string")
    || !(typeof value.fact_kind === "string" && FACT_KINDS.has(value.fact_kind))
    || !hasOwnField(value, "value")
    || !(typeof value.status === "string" && FACT_STATUSES.has(value.status))
  ) {
    throw new Error("Invalid Text-to-SQL semantic fact shape");
  }
  return {
    fact_key: value.fact_key,
    subject: value.subject as TextToSqlSemanticFact["subject"],
    table_fqn: value.table_fqn,
    column: value.column as string | null,
    fact_kind: value.fact_kind as TextToSqlSemanticFact["fact_kind"],
    value: value.value,
    status: value.status as TextToSqlSemanticFact["status"],
  };
}

export function parseTextToSqlMetadataView(payload: unknown): TextToSqlMetadataView {
  if (
    !isRecord(payload)
    || typeof payload.connection_ref !== "string"
    || !payload.connection_ref
    || (payload.schema_digest !== null && typeof payload.schema_digest !== "string")
    || (payload.editable_file_enabled !== null && typeof payload.editable_file_enabled !== "boolean")
    || !isRecord(payload.tables)
    || !isRecord(payload.glossary)
    || !Array.isArray(payload.facts)
  ) {
    throw new Error("Invalid Text-to-SQL metadata view shape");
  }

  const glossary = payload.glossary;
  if (
    typeof glossary.digest !== "string"
    || !glossary.digest
    || typeof glossary.profile_exists !== "boolean"
    || !Array.isArray(glossary.entries)
  ) {
    throw new Error("Invalid Text-to-SQL metadata glossary shape");
  }

  const tables: Record<string, TextToSqlTableMetadata> = {};
  for (const [tableFqn, table] of Object.entries(payload.tables)) {
    tables[tableFqn] = parseTableMetadata(table);
  }

  return {
    connection_ref: payload.connection_ref,
    schema_digest: payload.schema_digest,
    editable_file_enabled: payload.editable_file_enabled,
    tables,
    glossary: {
      digest: glossary.digest,
      profile_exists: glossary.profile_exists,
      entries: glossary.entries.map(parseGlossaryEntry),
    },
    facts: payload.facts.map(parseSemanticFact),
  };
}
