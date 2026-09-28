#!/usr/bin/env node
// Dependency vulnerability audit against npm's bulk advisory endpoint.
//
// The web CI jobs pin Node 22.22.2. pnpm stays at 10.23.0. pnpm 11 has moved
// `pnpm audit` to npm's bulk endpoint (upstream decision:
// https://github.com/orgs/pnpm/discussions/11377), but changing the
// package-manager major is a separate compatibility decision.
// Keep this stricter fail-closed wrapper while pnpm 10 is pinned: collect the
// installed dependency tree (production-only with --prod), submit
// name -> [versions] to
// /-/npm/v1/security/advisories/bulk, range-match each installed version
// against every advisory's vulnerable_versions, and fail closed at the
// configured severity threshold. Endpoint or parsing failures exit nonzero:
// an audit that cannot run must never pass silently.
import { execFileSync } from "node:child_process";
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { validateBulkAdvisoryResponse } from "./npm-advisory-response.mjs";

const require = createRequire(import.meta.url);
const semver = require("semver");

// Disclosed, expiring exceptions for advisories with no available remediation.
// Deliberately narrow: an entry names one advisory URL and one package, never
// applies to the production audit, and stops applying the moment it expires so
// it cannot quietly become permanent. Every applied entry prints on every run.
const EXCEPTIONS_PATH =
  process.env.NPM_ADVISORY_EXCEPTIONS_PATH ??
  path.join(
    path.dirname(path.dirname(fileURLToPath(import.meta.url))),
    "security-advisory-exceptions.json",
  );

function loadExceptions(now) {
  let raw;
  try {
    raw = readFileSync(EXCEPTIONS_PATH, "utf8");
  } catch (error) {
    if (error.code === "ENOENT") return [];
    throw new Error(`exception file unreadable: ${error.message}`);
  }
  let parsed;
  try {
    parsed = JSON.parse(raw);
  } catch (error) {
    throw new Error(`exception file is not valid JSON: ${error.message}`);
  }
  const entries = parsed?.exceptions;
  if (!Array.isArray(entries)) {
    throw new Error("exception file must contain an `exceptions` array");
  }
  return entries.map((entry, index) => {
    const where = `exception[${index}]`;
    for (const field of ["advisory_url", "package", "expires", "justification"]) {
      if (typeof entry?.[field] !== "string" || entry[field].trim() === "") {
        throw new Error(`${where} is missing a non-empty ${field}`);
      }
    }
    if (!/^\d{4}-\d{2}-\d{2}$/.test(entry.expires)) {
      throw new Error(`${where} expires must be YYYY-MM-DD, got ${entry.expires}`);
    }
    const expires = new Date(`${entry.expires}T23:59:59Z`);
    if (Number.isNaN(expires.getTime())) {
      throw new Error(`${where} expires is not a real date: ${entry.expires}`);
    }
    return { ...entry, expiresAt: expires, expired: expires < now };
  });
}

const BULK_ADVISORY_URL =
  process.env.NPM_BULK_ADVISORY_URL ??
  "https://registry.npmjs.org/-/npm/v1/security/advisories/bulk";
const SEVERITY_RANK = { info: 0, low: 1, moderate: 2, high: 3, critical: 4 };

const args = process.argv.slice(2);
const prodOnly = args.includes("--prod");
const levelArg = args.find((a) => a.startsWith("--audit-level="));
const auditLevel = levelArg ? levelArg.split("=")[1] : "high";
const threshold = SEVERITY_RANK[auditLevel];
if (threshold === undefined) {
  console.error(`unknown --audit-level=${auditLevel}`);
  process.exit(2);
}

function collectVersions() {
  const listArgs = ["list", "--json", "--depth", "Infinity"];
  if (prodOnly) listArgs.splice(1, 0, "--prod");
  const raw = execFileSync("pnpm", listArgs, {
    encoding: "utf8",
    maxBuffer: 256 * 1024 * 1024,
  });
  const tree = JSON.parse(raw);
  const versions = new Map();
  const visit = (deps) => {
    if (!deps) return;
    for (const [name, info] of Object.entries(deps)) {
      const version = info?.version;
      if (typeof version === "string" && semver.valid(version)) {
        if (!versions.has(name)) versions.set(name, new Set());
        versions.get(name).add(version);
      }
      visit(info?.dependencies);
    }
  };
  for (const project of tree) {
    visit(project.dependencies);
    if (!prodOnly) {
      visit(project.devDependencies);
      visit(project.optionalDependencies);
    }
  }
  return versions;
}

async function fetchAdvisories(versions) {
  const body = {};
  for (const [name, set] of versions) body[name] = [...set];
  const response = await fetch(BULK_ADVISORY_URL, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`bulk advisory endpoint returned HTTP ${response.status}`);
  }
  return validateBulkAdvisoryResponse(await response.json());
}

const versions = collectVersions();
if (versions.size === 0) {
  console.error("no installed dependencies found; refusing to pass an empty audit");
  process.exit(2);
}

let advisories;
try {
  advisories = await fetchAdvisories(versions);
} catch (error) {
  console.error(`audit failed to run: ${error.message}`);
  process.exit(2);
}

const findings = [];
for (const [name, entries] of Object.entries(advisories)) {
  const installed = versions.get(name);
  if (!installed) continue;
  for (const advisory of entries) {
    const range = advisory.vulnerable_versions ?? "*";
    const affected = [...installed].filter((v) =>
      semver.satisfies(v, range, { includePrerelease: true }),
    );
    if (affected.length === 0) continue;
    const severity = String(advisory.severity ?? "info").toLowerCase();
    findings.push({
      name,
      affected,
      severity,
      rank: SEVERITY_RANK[severity] ?? SEVERITY_RANK.critical,
      title: advisory.title ?? "(untitled advisory)",
      url: advisory.url ?? "",
      range,
    });
  }
}

const scope = prodOnly ? "production" : "full";

// The production audit never consults exceptions. Anything reaching shipped
// artifacts blocks unconditionally.
let exceptions = [];
if (!prodOnly) {
  try {
    exceptions = loadExceptions(new Date());
  } catch (error) {
    console.error(`audit failed to run: ${error.message}`);
    process.exit(2);
  }
  for (const entry of exceptions.filter((e) => e.expired)) {
    console.error(
      `[EXPIRED] exception for ${entry.package} ${entry.advisory_url} lapsed on ` +
        `${entry.expires} and no longer applies; re-check the advisory and either ` +
        `remediate or renew it deliberately`,
    );
  }
}

const liveExceptions = exceptions.filter((e) => !e.expired);
const exceptionFor = (finding) =>
  liveExceptions.find(
    (e) => e.advisory_url === finding.url && e.package === finding.name,
  );

let excepted = 0;
const blocking = [];
for (const f of findings) {
  if (f.rank < threshold) {
    console.log(`[info] ${f.severity} ${f.name}@${f.affected.join(",")} (${f.range}) ${f.title} ${f.url}`);
    continue;
  }
  const entry = exceptionFor(f);
  if (entry) {
    excepted += 1;
    console.log(
      `[EXCEPTED until ${entry.expires}] ${f.severity} ${f.name}@${f.affected.join(",")} ` +
        `(${f.range}) ${f.title} ${f.url}\n    justification: ${entry.justification}`,
    );
    continue;
  }
  blocking.push(f);
  console.log(
    `[BLOCKING] ${f.severity} ${f.name}@${f.affected.join(",")} (${f.range}) ${f.title} ${f.url}`,
  );
}

// An exception that stops matching is stale scaffolding; say so rather than
// leaving a permanent entry nobody revisits.
for (const entry of liveExceptions) {
  if (!findings.some((f) => f.url === entry.advisory_url && f.name === entry.package)) {
    console.log(
      `[STALE] exception for ${entry.package} ${entry.advisory_url} matched nothing; ` +
        `it can be removed`,
    );
  }
}

const expiredCount = exceptions.filter((e) => e.expired).length;
console.log(
  `${scope} audit: ${versions.size} packages checked, ` +
    `${findings.length} matching advisories, ${blocking.length} at or above ${auditLevel}` +
    (excepted > 0 ? `, ${excepted} excepted` : "") +
    (expiredCount > 0 ? `, ${expiredCount} expired exception(s)` : ""),
);
process.exit(blocking.length > 0 || expiredCount > 0 ? 1 : 0);
