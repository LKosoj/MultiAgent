import { describe, expect, it } from "vitest";

import { createTextToSqlClient } from "../textToSqlClient";

const noopStart = () => Promise.reject(new Error("start() is unused in this test"));

describe("createTextToSqlClient metadata actions", () => {
  it("loadMetadata calls text_to_sql.metadata.load with the connection_ref", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const client = createTextToSqlClient(
      async (action, payload) => {
        calls.push([action, payload]);
        return { ok: true };
      },
      noopStart,
    );

    await client.loadMetadata("conn-1");

    expect(calls).toEqual([["text_to_sql.metadata.load", { connection_ref: "conn-1" }]]);
  });

  it("saveMetadataDescriptions forwards the payload to text_to_sql.metadata.save_descriptions unchanged", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const client = createTextToSqlClient(
      async (action, payload) => {
        calls.push([action, payload]);
        return { saved: true, schema_digest: "digest" };
      },
      noopStart,
    );
    const payload = {
      connection_ref: "conn-1",
      expected_schema_digest: "old-digest",
      tables: [{ table_fqn: "public.orders", description: "desc" }],
    };

    const result = await client.saveMetadataDescriptions(payload);

    expect(calls).toEqual([["text_to_sql.metadata.save_descriptions", payload]]);
    expect(result).toEqual({ saved: true, schema_digest: "digest" });
  });

  it("saveMetadataGlossary forwards the payload to text_to_sql.metadata.save_glossary unchanged", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const client = createTextToSqlClient(
      async (action, payload) => {
        calls.push([action, payload]);
        return { saved: true, glossary_digest: "digest" };
      },
      noopStart,
    );
    const payload = {
      connection_ref: "conn-1",
      expected_glossary_digest: "old-digest",
      entries: [{ term: "t", synonyms: [], table: "public.orders", column: null, kind: null, note: null }],
    };

    const result = await client.saveMetadataGlossary(payload);

    expect(calls).toEqual([["text_to_sql.metadata.save_glossary", payload]]);
    expect(result).toEqual({ saved: true, glossary_digest: "digest" });
  });

  it("setMetadataFactStatus forwards the payload to text_to_sql.metadata.set_fact_status unchanged", async () => {
    const calls: Array<[string, Record<string, unknown>]> = [];
    const client = createTextToSqlClient(
      async (action, payload) => {
        calls.push([action, payload]);
        return { saved: true, fact_key: "fact-1", status: "rejected" };
      },
      noopStart,
    );
    const payload = { connection_ref: "conn-1", fact_key: "fact-1", status: "rejected" };

    const result = await client.setMetadataFactStatus(payload);

    expect(calls).toEqual([["text_to_sql.metadata.set_fact_status", payload]]);
    expect(result).toEqual({ saved: true, fact_key: "fact-1", status: "rejected" });
  });

  it("propagates a version-conflict error from save_descriptions unchanged", async () => {
    const client = createTextToSqlClient(
      async () => {
        throw new Error("metadata version conflict: reload table/column metadata before saving");
      },
      noopStart,
    );

    await expect(client.saveMetadataDescriptions({ connection_ref: "conn-1" }))
      .rejects.toThrow("version conflict");
  });
});
