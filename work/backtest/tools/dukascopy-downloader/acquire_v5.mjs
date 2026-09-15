import * as locked from "dukascopy-node";
import { createHash, randomUUID } from "node:crypto";
import { createReadStream, promises as fs } from "node:fs";
import { createInterface } from "node:readline";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

const { generateUrls, BufferFetcher, processData, formatOutput } = locked;
const ROOT = path.dirname(fileURLToPath(import.meta.url));
const LOCKFILE = path.join(ROOT, "package-lock.json");
const PACKAGE = path.join(ROOT, "node_modules", "dukascopy-node", "package.json");
const EXPECTED_PACKAGE = "dukascopy-node";
const EXPECTED_VERSION = "1.50.0";
const EXPECTED_LOCK_SHA256 = "74506876532b3d1caf4be740fc42c79e9eb529518770afd05c40a0463c732dee";
const EXPECTED_INTEGRITY = "sha512-o2Co/asUD/TXFNhblJUYkRseHMt/uvFrnhzOKWezLuiFJqbl4Zn2oJGL4/W+PY1b2YsI11+9+TO40qNQBAj8/w==";
const ALLOWED_HOST = "datafeed.dukascopy.com";
const MAX_BODY_BYTES = 16 * 1024 * 1024;

const sha256 = (body) => createHash("sha256").update(body).digest("hex");
const jsonHash = (value) => sha256(Buffer.from(JSON.stringify(value, Object.keys(value).sort())));

async function lockedContract() {
  const lockBytes = await fs.readFile(LOCKFILE);
  if (sha256(lockBytes) !== EXPECTED_LOCK_SHA256) throw new Error("package-lock SHA mismatch");
  const pkg = JSON.parse(await fs.readFile(PACKAGE, "utf8"));
  if (pkg.name !== EXPECTED_PACKAGE || pkg.version !== EXPECTED_VERSION) throw new Error("dukascopy-node package version mismatch");
  const lock = JSON.parse(lockBytes);
  const node = lock.packages?.["node_modules/dukascopy-node"];
  if (!node || node.version !== EXPECTED_VERSION || node.integrity !== EXPECTED_INTEGRITY) throw new Error("dukascopy-node integrity mismatch");
  if (![generateUrls, BufferFetcher, processData, formatOutput].every(Boolean)) throw new Error("locked dukascopy-node primitives are unavailable");
  return { package: EXPECTED_PACKAGE, version: EXPECTED_VERSION, integrity: EXPECTED_INTEGRITY, lockfile_sha256: EXPECTED_LOCK_SHA256 };
}

function parseDate(value, label) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) throw new Error(`${label} is not a valid date`);
  return date;
}

function planInput(args) {
  const instrument = String(args.instrument || "").toLowerCase();
  const timeframe = String(args.timeframe || "m1").toLowerCase();
  const priceType = String(args["price-type"] || "bid").toLowerCase();
  if (!instrument || !args.start || !args.end || timeframe !== "m1" || priceType !== "bid") throw new Error("plan requires instrument, start, end, timeframe=m1 and price-type=bid");
  return { instrument, timeframe, priceType, startDate: parseDate(args.start, "start"), endDate: parseDate(args.end, "end") };
}

function planFromInput(input) {
  const generated = generateUrls(input);
  const urls = generated.map((value) => {
    const parsed = new URL(value);
    parsed.hostname = ALLOWED_HOST;
    parsed.username = "";
    parsed.password = "";
    parsed.hash = "";
    parsed.port = "";
    return parsed.toString();
  });
  return { instrument: input.instrument, timeframe: input.timeframe, priceType: input.priceType, start: input.startDate.toISOString(), end: input.endDate.toISOString(), urls, url_sha256: urls.map((url) => sha256(Buffer.from(url))) };
}

function validatePlan(plan) {
  if (!plan || typeof plan !== "object" || !plan.instrument || !plan.start || !plan.end || !Array.isArray(plan.urls) || !Array.isArray(plan.url_sha256)) throw new Error("locked plan is incomplete");
  const regenerated = planFromInput({ instrument: plan.instrument, timeframe: plan.timeframe, priceType: plan.priceType, startDate: parseDate(plan.start, "plan start"), endDate: parseDate(plan.end, "plan end") });
  if (JSON.stringify(regenerated.urls) !== JSON.stringify(plan.urls) || JSON.stringify(regenerated.url_sha256) !== JSON.stringify(plan.url_sha256)) throw new Error("locked plan does not match generateUrls output");
  return plan;
}

function parseArgs(argv) {
  const [mode, ...rest] = argv;
  const args = { mode };
  for (let index = 0; index < rest.length; index += 1) {
    const item = rest[index];
    if (!item.startsWith("--")) throw new Error(`unexpected argument ${item}`);
    const key = item.slice(2);
    if (key === "json") args.json = rest[++index];
    else if (["output", "plan", "stage-root", "cas-root", "input", "index", "url-sha256", "instrument", "start", "end", "timeframe", "price-type"].includes(key)) args[key] = rest[++index];
    else if (key === "url") throw new Error("free --url input is not permitted; use a locked plan");
    else throw new Error(`unknown argument --${key}`);
  }
  if (!["plan", "fetch-one", "decode"].includes(args.mode)) throw new Error("mode must be plan, fetch-one or decode");
  return args;
}

function validateUrl(value) {
  const parsed = new URL(value);
  if (parsed.protocol !== "https:" || parsed.hostname.toLowerCase() !== ALLOWED_HOST || parsed.username || parsed.password || parsed.hash || parsed.port) throw new Error("provider URL is outside the locked HTTPS allowlist");
  return parsed;
}

function safeEndpoint(value) {
  const parsed = validateUrl(value);
  return `${parsed.protocol}//${parsed.host}${parsed.pathname}`;
}

async function fetchWithLimits(url, stageRoot, meta, timeoutMs = 30_000) {
  const parsed = validateUrl(url);
  const controller = new AbortController();
  if (!Number.isInteger(timeoutMs) || timeoutMs <= 0) throw new Error("provider timeout must be a positive integer");
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  let response;
  let reader;
  let complete = false;
  try {
    response = await fetch(parsed, { redirect: "error", signal: controller.signal });
    const allowedHeaders = {};
    for (const name of ["retry-after", "date", "content-type", "content-length", "etag"]) if (response.headers.has(name)) allowedHeaders[name] = response.headers.get(name);
    const declared = Number(allowedHeaders["content-length"] || 0);
    if (!Number.isFinite(declared) || declared < 0 || declared > MAX_BODY_BYTES) throw new Error("provider body exceeds 16 MiB");
    const chunks = [];
    let size = 0;
    if (!response.body) throw new Error("provider response has no body");
    reader = response.body.getReader();
    while (true) {
      const next = await reader.read();
      if (next.done) break;
      size += next.value.byteLength;
      if (size > MAX_BODY_BYTES) throw new Error("provider body exceeds 16 MiB");
      chunks.push(Buffer.from(next.value));
    }
    const body = Buffer.concat(chunks);
    const bodySha = sha256(body);
    await fs.mkdir(stageRoot, { recursive: true });
    const staging = path.resolve(stageRoot, `${bodySha}.${process.pid}.${randomUUID()}.part`);
    await fs.writeFile(staging, body, { flag: "wx" });
    meta.push({ status: response.status, headers: allowedHeaders, endpoint: safeEndpoint(url), byte_count: body.length, body_sha256: bodySha, staging_path: staging, url_sha256: sha256(Buffer.from(url)) });
    complete = true;
    return body;
  } finally {
    clearTimeout(timer);
    if (!complete && reader) await reader.cancel().catch(() => {});
  }
}

async function readJson(pathname) {
  return JSON.parse(await fs.readFile(pathname, "utf8"));
}

async function runPlan(args) {
  const input = planInput(args);
  const plan = planFromInput(input);
  if (args.output) await fs.writeFile(args.output, JSON.stringify(plan, null, 2) + "\n", { encoding: "utf8", flag: "w" });
  return plan;
}

async function runFetchOne(args) {
  const plan = validatePlan(await readJson(args.plan));
  const index = Number(args.index);
  if (!Number.isInteger(index) || index < 0 || index >= plan.urls.length) throw new Error("fetch-one index is outside the plan");
  const url = plan.urls[index];
  if (!/^[0-9a-f]{64}$/.test(String(args["url-sha256"] || "")) || args["url-sha256"] !== sha256(Buffer.from(url))) throw new Error("fetch-one URL binding mismatch");
  const meta = [];
  const fetcher = new BufferFetcher({ batchSize: 1, retryCount: 0, retryOnEmpty: false, fetcherFn: (item) => fetchWithLimits(item, args["stage-root"], meta) });
  const result = await fetcher.fetch([url]);
  const evidence = { mode: "fetch-one", plan_url_index: index, url_sha256: sha256(Buffer.from(url)), ...meta[0], package: await lockedContract() };
  return { ...evidence, buffer_sha256: sha256(result[0].buffer) };
}

async function runDecode(args) {
  const contract = await lockedContract();
  const plan = validatePlan(await readJson(args.plan));
  const input = await readJson(args.input);
  const indexes = input.map((item) => Number(item.index));
  if (indexes.some((value, index) => !Number.isInteger(value) || value < 0 || value >= plan.urls.length || (index > 0 && value <= indexes[index - 1]))) throw new Error("decode CAS list is not an ordered plan subset");
  const objects = [];
  for (const item of input) {
    const body = await fs.readFile(item.path);
    if (!item.path || item.sha256 !== sha256(body)) throw new Error("decode CAS body binding mismatch");
    objects.push({ url: plan.urls[Number(item.index)], buffer: body });
  }
  const processedData = processData({ instrument: plan.instrument, requestedTimeframe: "m1", bufferObjects: objects, priceType: "bid", volumes: true, volumeUnits: "units", ignoreFlats: false });
  const formatted = formatOutput({ processedData, timeframe: "m1", format: "csv" });
  if (args.output) await fs.writeFile(args.output, formatted, { encoding: "utf8", flag: "wx" });
  return { mode: "decode", rows: processedData.length, output_sha256: sha256(Buffer.from(formatted)), package: contract };
}

if (process.argv[1] && import.meta.url === pathToFileURL(path.resolve(process.argv[1])).href) {
  try {
    const args = parseArgs(process.argv.slice(2));
    const contract = await lockedContract();
    const result = args.mode === "plan" ? await runPlan(args) : args.mode === "fetch-one" ? await runFetchOne(args) : await runDecode(args);
    process.stdout.write(JSON.stringify({ ...result, contract }) + "\n");
  } catch (error) {
    process.stderr.write(JSON.stringify({ error: String(error?.message || error) }) + "\n");
    process.exitCode = 1;
  }
}

export { fetchWithLimits };
