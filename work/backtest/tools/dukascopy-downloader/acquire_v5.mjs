import * as locked from "dukascopy-node";

const { generateUrls, BufferFetcher, processData, formatOutput } = locked;
const ALLOWED_HOSTS = new Set(["datafeed.dukascopy.com"]);

function validateUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || !ALLOWED_HOSTS.has(parsed.hostname.toLowerCase())) {
    throw new Error("provider URL is outside the locked HTTPS allowlist");
  }
  if (parsed.username || parsed.password || parsed.hash) {
    throw new Error("provider URL contains forbidden credentials or fragment");
  }
  return parsed;
}

function safeEndpoint(value) {
  const parsed = validateUrl(value);
  return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
}

async function fetchNative(url) {
  const parsed = validateUrl(url);
  const response = await fetch(parsed, { redirect: "error" });
  const body = Buffer.from(await response.arrayBuffer());
  return {
    body,
    status: response.status,
    headers: Object.fromEntries(response.headers.entries()),
    endpoint: safeEndpoint(url),
  };
}

export function lockedPrimitiveContract() {
  return {
    generateUrls,
    BufferFetcher,
    processData,
    formatOutput,
    package: "dukascopy-node",
    version: "1.50.0",
    batchSize: 1,
    retryCount: 0,
  };
}

export async function fetchArtifact(url) {
  // Keep these locked primitives part of the execution contract.  Native fetch
  // is deliberately the transport so status and response headers are retained.
  const primitives = lockedPrimitiveContract();
  if (!primitives.generateUrls || !primitives.BufferFetcher || !primitives.processData || !primitives.formatOutput) {
    throw new Error("locked dukascopy-node primitives are unavailable");
  }
  const result = await fetchNative(url);
  return {
    body: result.body,
    status: result.status,
    headers: result.headers,
    endpoint: result.endpoint,
    package: primitives.package,
    version: primitives.version,
  };
}

function parseArgs(argv) {
  const index = argv.indexOf("--url");
  if (index < 0 || !argv[index + 1]) throw new Error("--url is required");
  return { url: argv[index + 1] };
}

if (import.meta.url === `file://${process.argv[1].replaceAll("\\", "/")}`) {
  try {
    const result = await fetchArtifact(parseArgs(process.argv.slice(2)).url);
    process.stdout.write(JSON.stringify({
      status: result.status,
      headers: result.headers,
      endpoint: result.endpoint,
      body_base64: result.body.toString("base64"),
    }) + "\n");
    process.exitCode = result.status >= 400 ? 1 : 0;
  } catch (error) {
    process.stderr.write(JSON.stringify({ error: String(error?.message || error) }) + "\n");
    process.exitCode = 1;
  }
}
