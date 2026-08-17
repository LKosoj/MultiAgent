import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { describe, expect, it } from "vitest";

import {
  buildDbConnectionRegistrationPayload,
  dbConnectionEntries,
} from "../DbSection";
import {
  buildTextToSqlSchemaPayload,
  buildTextToSqlStartPayload,
  textToSqlConnectionEntries,
} from "../TextToSqlSection";

const connectionRef = "conn-123e4567-e89b-42d3-a456-426614174000";

describe("ordinary Text-to-SQL connection boundary", () => {
  it("starts with a connection reference and no raw-DSN compatibility fields", () => {
    const payload = buildTextToSqlStartPayload({
      query: "count orders",
      connectionRef,
      maxRows: 100,
      sessionId: "session-1",
      enableTelemetry: true,
      safetyLevel: "strict",
      includeExplanation: true,
      validateSchema: true,
      dryRunOnly: false,
    });

    expect(payload).toMatchObject({
      query: "count orders",
      connection_ref: connectionRef,
    });
    expect(payload).not.toHaveProperty("natural_query");
    expect(payload).not.toHaveProperty("dsn");
    expect(payload).not.toHaveProperty("admin_raw_dsn_compat");
    expect(payload).not.toHaveProperty("persistence");
  });

  it("rejects a raw connection string supplied as a reference", () => {
    expect(() => buildTextToSqlStartPayload({
      query: "count orders",
      connectionRef: "postgresql://user:secret@db.example/app",
      maxRows: 100,
      enableTelemetry: false,
      safetyLevel: "strict",
      includeExplanation: true,
      validateSchema: true,
      dryRunOnly: false,
    })).toThrow("Select an authorized database connection");
  });

  it("loads schema through the same reference-only contract", () => {
    const payload = buildTextToSqlSchemaPayload(
      connectionRef,
      "public",
      "orders",
      false,
    );

    expect(payload).toEqual({
      connection_ref: connectionRef,
      schema: "public",
      table_name: "orders",
      allow_db_schema_fallback: false,
    });
    expect(payload).not.toHaveProperty("dsn");
  });

  it("retains only public connection metadata from list responses", () => {
    const entries = textToSqlConnectionEntries({
      connections: [{
        connection_ref: connectionRef,
        display_name: "Orders",
        owner_subject: "alice",
        tenant_id: "tenant-a",
        target_description: "postgresql at db.example:5432",
        dialect: "postgresql",
        enabled_for_user: true,
        dsn: "postgresql://alice:secret@db.example:5432/orders",
      }],
    });

    expect(entries).toEqual([{
      connection_ref: connectionRef,
      display_name: "Orders",
      owner_subject: "alice",
      tenant_id: "tenant-a",
      target_description: "postgresql at db.example:5432",
      dialect: "postgresql",
      enabled_for_user: true,
    }]);
    expect(JSON.stringify(entries)).not.toContain("secret");
    expect(entries[0]).not.toHaveProperty("dsn");
  });

  it("has no ordinary connection persistence or legacy config actions", () => {
    const source = readFileSync(fileURLToPath(new URL(
      "../TextToSqlSection.tsx",
      import.meta.url,
    )), "utf8");
    const clientSource = readFileSync(fileURLToPath(new URL(
      "../../../lib/textToSqlClient.ts",
      import.meta.url,
    )), "utf8");

    expect(clientSource).toContain('runServiceAction("db.connections.list", {})');
    expect(source).not.toContain("db.test_configs");
    expect(source).not.toContain("localStorage");
    expect(source).not.toContain("sessionStorage");
    expect(source).not.toContain("indexedDB");
    expect(source).not.toContain("localConnectionDsnsRef");
  });
});

describe("admin DB connection boundary", () => {
  it("registers an explicit owner and tenant", () => {
    expect(buildDbConnectionRegistrationPayload({
      displayName: " Orders ",
      dsn: "postgresql://admin:secret@db.example:5432/orders",
      ownerSubject: " alice ",
      tenantId: " tenant-a ",
    })).toEqual({
      display_name: "Orders",
      dsn: "postgresql://admin:secret@db.example:5432/orders",
      owner_subject: "alice",
      tenant_id: "tenant-a",
    });
  });

  it("uses a null owner for an explicit tenant-wide registration", () => {
    expect(buildDbConnectionRegistrationPayload({
      displayName: "Orders",
      dsn: "postgresql://admin:secret@db.example:5432/orders",
      ownerSubject: " ",
      tenantId: "tenant-a",
    })).toMatchObject({
      owner_subject: null,
      tenant_id: "tenant-a",
    });
  });

  it("does not retain registration secrets from list responses", () => {
    const entries = dbConnectionEntries({
      result: {
        connections: [{
          connection_ref: connectionRef,
          display_name: "Orders",
          owner_subject: "alice",
          tenant_id: "tenant-a",
          target_description: "postgresql at db.example:5432",
          dsn: "postgresql://admin:secret@db.example:5432/orders",
        }],
      },
    });

    expect(entries[0]).not.toHaveProperty("dsn");
    expect(JSON.stringify(entries)).not.toContain("secret");
  });

  it("uses registry actions for list, registration, and deletion", () => {
    const source = readFileSync(fileURLToPath(new URL(
      "../DbSection.tsx",
      import.meta.url,
    )), "utf8");

    expect(source).toContain('runServiceAction("db.connections.list", {})');
    expect(source).toContain('"db.connections.register"');
    expect(source).toContain('runServiceAction("db.connections.delete"');
  });
});
