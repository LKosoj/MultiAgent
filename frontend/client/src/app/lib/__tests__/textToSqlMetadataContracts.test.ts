import { describe, expect, it } from "vitest";

import { parseTextToSqlMetadataView } from "../textToSqlMetadataContracts";

// Mirrors the text_to_sql.metadata.load response shape from
// docs/plans/2026-09-05-text2sql-metadata-editor.md §1.2. The backend for
// this contract may not exist yet (frontend and backend were built in
// parallel from the same design doc), so these vectors are hand-built here
// rather than shared with a Python-side fixture.
function validPayload(): Record<string, unknown> {
  return {
    connection_ref: "conn-11111111-1111-1111-8111-111111111111",
    dsn_dialect: "postgresql",
    schema_digest: "a".repeat(64),
    editable_file_enabled: true,
    tables: {
      "public.orders": {
        description: "Customer orders",
        description_source: "file",
        columns: {
          status: {
            type: "text",
            description: "Order status",
            description_source: "file",
            examples: ["paid", "pending"],
            examples_source: "file",
          },
          amount: {
            description: "",
            description_source: "none",
            examples: [],
            examples_source: "none",
          },
        },
      },
    },
    glossary: {
      digest: "b".repeat(64),
      profile_exists: true,
      dsn_fingerprint: "postgresql://host:5432/db",
      schema_namespace_version: "c".repeat(64),
      entries: [
        {
          term: "выручка",
          synonyms: ["revenue", "оборот"],
          table: "public.orders",
          column: "amount",
          kind: "measure",
          note: null,
        },
      ],
    },
    facts: [
      {
        fact_key: "text2sql-semantic-fact-v1-abc",
        subject: "column",
        table_fqn: "public.orders",
        column: "status",
        fact_kind: "description",
        value: "typed-probe observed value",
        status: "approved",
      },
    ],
  };
}

describe("parseTextToSqlMetadataView", () => {
  it("parses a valid backend payload into the trimmed frontend view", () => {
    const view = parseTextToSqlMetadataView(validPayload());

    expect(view).toEqual({
      connection_ref: "conn-11111111-1111-1111-8111-111111111111",
      schema_digest: "a".repeat(64),
      editable_file_enabled: true,
      tables: {
        "public.orders": {
          description: "Customer orders",
          description_source: "file",
          columns: {
            status: {
              type: "text",
              description: "Order status",
              description_source: "file",
              examples: ["paid", "pending"],
              examples_source: "file",
            },
            amount: {
              description: "",
              description_source: "none",
              examples: [],
              examples_source: "none",
            },
          },
        },
      },
      glossary: {
        digest: "b".repeat(64),
        profile_exists: true,
        entries: [
          {
            term: "выручка",
            synonyms: ["revenue", "оборот"],
            table: "public.orders",
            column: "amount",
            kind: "measure",
            note: null,
          },
        ],
      },
      facts: [
        {
          fact_key: "text2sql-semantic-fact-v1-abc",
          subject: "column",
          table_fqn: "public.orders",
          column: "status",
          fact_kind: "description",
          value: "typed-probe observed value",
          status: "approved",
        },
      ],
    });
  });

  it("accepts null schema_digest and editable_file_enabled (no file yet)", () => {
    const payload = validPayload();
    payload.schema_digest = null;
    payload.editable_file_enabled = null;

    const view = parseTextToSqlMetadataView(payload);

    expect(view.schema_digest).toBeNull();
    expect(view.editable_file_enabled).toBeNull();
  });

  it("accepts a glossary entry with null column/kind/note", () => {
    const payload = validPayload();
    (payload.glossary as Record<string, unknown>).entries = [
      { term: "term", synonyms: [], table: "public.orders", column: null, kind: null, note: null },
    ];

    const view = parseTextToSqlMetadataView(payload);

    expect(view.glossary.entries).toEqual([
      { term: "term", synonyms: [], table: "public.orders", column: null, kind: null, note: null },
    ]);
  });

  it("ignores unknown top-level and nested fields (forward compatible)", () => {
    const payload = validPayload();
    payload.some_future_field = "unused";
    expect(() => parseTextToSqlMetadataView(payload)).not.toThrow();
  });

  it.each([
    ["missing connection_ref", (p: Record<string, unknown>) => { delete p.connection_ref; }],
    ["empty connection_ref", (p: Record<string, unknown>) => { p.connection_ref = ""; }],
    ["non-string schema_digest", (p: Record<string, unknown>) => { p.schema_digest = 7; }],
    ["non-boolean editable_file_enabled", (p: Record<string, unknown>) => { p.editable_file_enabled = "yes"; }],
    ["tables not a record", (p: Record<string, unknown>) => { p.tables = []; }],
    ["glossary not a record", (p: Record<string, unknown>) => { p.glossary = null; }],
    ["facts not an array", (p: Record<string, unknown>) => { p.facts = {}; }],
    ["glossary missing digest", (p: Record<string, unknown>) => { delete (p.glossary as Record<string, unknown>).digest; }],
    ["glossary empty digest", (p: Record<string, unknown>) => { (p.glossary as Record<string, unknown>).digest = ""; }],
    ["glossary entries not an array", (p: Record<string, unknown>) => { (p.glossary as Record<string, unknown>).entries = {}; }],
  ])("rejects a malformed payload: %s", (_name, mutate) => {
    const payload = validPayload();
    mutate(payload);
    expect(() => parseTextToSqlMetadataView(payload)).toThrow();
  });

  it.each([
    ["invalid description_source", (p: Record<string, unknown>) => {
      ((p.tables as Record<string, unknown>)["public.orders"] as Record<string, unknown>).description_source = "database";
    }],
    ["column missing examples array", (p: Record<string, unknown>) => {
      const table = (p.tables as Record<string, unknown>)["public.orders"] as Record<string, unknown>;
      ((table.columns as Record<string, unknown>).status as Record<string, unknown>).examples = "not-an-array";
    }],
  ])("rejects a malformed table/column shape: %s", (_name, mutate) => {
    const payload = validPayload();
    mutate(payload);
    expect(() => parseTextToSqlMetadataView(payload)).toThrow();
  });

  it.each([
    ["non-string synonyms entry", (p: Record<string, unknown>) => {
      (p.glossary as Record<string, unknown>).entries = [
        { term: "t", synonyms: [7], table: "public.orders", column: null, kind: null, note: null },
      ];
    }],
    ["invalid kind", (p: Record<string, unknown>) => {
      (p.glossary as Record<string, unknown>).entries = [
        { term: "t", synonyms: [], table: "public.orders", column: null, kind: "unknown_kind", note: null },
      ];
    }],
  ])("rejects a malformed glossary entry: %s", (_name, mutate) => {
    const payload = validPayload();
    mutate(payload);
    expect(() => parseTextToSqlMetadataView(payload)).toThrow();
  });

  it.each([
    ["invalid status", (p: Record<string, unknown>) => {
      p.facts = [{ ...(p.facts as Record<string, unknown>[])[0], status: "maybe" }];
    }],
    ["invalid subject", (p: Record<string, unknown>) => {
      p.facts = [{ ...(p.facts as Record<string, unknown>[])[0], subject: "row" }];
    }],
    ["invalid fact_kind", (p: Record<string, unknown>) => {
      p.facts = [{ ...(p.facts as Record<string, unknown>[])[0], fact_kind: "opinion" }];
    }],
    ["missing value field", (p: Record<string, unknown>) => {
      const fact = { ...(p.facts as Record<string, unknown>[])[0] };
      delete fact.value;
      p.facts = [fact];
    }],
    ["empty fact_key", (p: Record<string, unknown>) => {
      p.facts = [{ ...(p.facts as Record<string, unknown>[])[0], fact_key: "" }];
    }],
  ])("rejects a malformed semantic fact: %s", (_name, mutate) => {
    const payload = validPayload();
    mutate(payload);
    expect(() => parseTextToSqlMetadataView(payload)).toThrow();
  });
});
