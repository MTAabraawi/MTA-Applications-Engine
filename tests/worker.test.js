import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash, webcrypto } from "node:crypto";
import worker from "../worker.js";

globalThis.crypto ??= webcrypto;

test("Worker resolves exact source IDs independently of alphabetical catalogue pages", async () => {
  const source = "fdroid";
  const externalId = "org.example.target";
  const key = source + ":" + externalId;
  const shard = (createHash("sha256").update(key).digest().readUInt32BE(0) % 256)
    .toString().padStart(4, "0");
  const record = {
    source_key: source,
    external_id: externalId,
    platform: "android",
    name: "Target App",
    source_url: "https://example.org/target"
  };
  const manifest = {
    schema_version: 1,
    storage: "git-json-shards",
    search_index_format: "prefix2-3-v2",
    shard_count: 256
  };
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async input => {
    const url = String(input);
    if (url.endsWith("/catalogue_data/manifest.json")) {
      return new Response(JSON.stringify(manifest), { status: 200 });
    }
    if (url.endsWith("/catalogue_data/shards/" + shard + ".json")) {
      return new Response(JSON.stringify({ [key]: record }), { status: 200 });
    }
    return new Response("{}", { status: 404 });
  };

  try {
    const env = {
      CATALOGUE_STORAGE: "github",
      GITHUB_CATALOGUE_BRANCH: "test-exact-source-id"
    };
    const found = await worker.fetch(
      new Request("https://engine.test/api/v1/apps?source=fdroid&external_id=org.example.target"),
      env
    );
    assert.equal(found.status, 200);
    const rows = await found.json();
    assert.equal(rows.length, 1);
    assert.equal(rows[0].external_id, externalId);
    assert.equal(rows[0].name, "Target App");

    const missing = await worker.fetch(
      new Request("https://engine.test/api/v1/apps?source=fdroid&external_id=org.example.missing"),
      env
    );
    assert.deepEqual(await missing.json(), []);

    const unscoped = await worker.fetch(
      new Request("https://engine.test/api/v1/apps?external_id=org.example.target"),
      env
    );
    assert.deepEqual(await unscoped.json(), []);
  } finally {
    globalThis.fetch = originalFetch;
  }
});
