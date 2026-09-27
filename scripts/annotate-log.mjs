#!/usr/bin/env node
/**
 * annotate-log.mjs — turn a failed test log into GitHub annotations.
 *
 *   node scripts/annotate-log.mjs smoke-web.log smoke-worker.log
 *   node scripts/annotate-log.mjs --notice audit.log
 *
 * Why this exists: a red job whose only evidence is "Process completed with exit
 * code 1" cannot be triaged from the run page, and the job log itself is on a host
 * that not every environment can reach (a sandbox, a mirror, a firewalled
 * workstation). Annotations travel with the run through the API, so the failing
 * check's own words reach whoever is looking — including an agent.
 *
 * Two log shapes are understood, because the two suites print differently:
 *
 *   the smoke suites      FAIL <check>
 *                         <what happened>
 *                         <expected !== actual>
 *
 *   the audit             [bugs] id
 *                         location @ file:line
 *                         message
 *                         -> remedy
 *
 * Everything else is ignored, which is deliberate: a log with no failure in it
 * produces no annotation rather than a wall of noise.
 *
 * Workflow commands need their own escaping for the fields they carry; getting
 * that wrong prints the message but truncates it at the first newline, which is
 * exactly the line an analyst needs.
 */

import { readFileSync } from "node:fs";

/** Escape a workflow-command *data* field: %, CR and LF. */
export function commandData(text) {
  return String(text)
    .replace(/%/g, "%25")
    .replace(/\r/g, "%0D")
    .replace(/\n/g, "%0A");
}

/** Escape a workflow-command *title*: the data rules plus `:` and `,`. */
export function commandTitle(text) {
  return commandData(text).replace(/:/g, "%3A").replace(/,/g, "%2C");
}

/** Pull the findings out of one suite's output. */
export function extractFindings(text) {
  const lines = String(text).split(/\r?\n/);
  const findings = [];
  for (let i = 0; i < lines.length; i += 1) {
    const line = lines[i];
    const smoke = /^FAIL\s+(.*)$/.exec(line);
    if (smoke) {
      // The suites print the first six lines of the error, so the assertion's own
      // words ("200 !== 400", "expected … actual …") sit a line or two below the
      // message, sometimes after a blank line. Stack frames are noise here; the
      // message and the diff are the evidence.
      const detail = [];
      for (let j = i + 1; j < lines.length && detail.length < 8; j += 1) {
        const next = lines[j];
        if (/^\s*(ok|FAIL)\s/.test(next) || /smoke test: \d+ checks/.test(next)) break;
        if (/^\s+at\s/.test(next) || /^\s*$/.test(next)) continue;
        detail.push(next.trim());
      }
      findings.push({ title: smoke[1].trim(), message: detail.join(" | ").slice(0, 600) });
      continue;
    }
    const audit = /^ {2}\[(?<dimension>[a-z]+)\]\s+(?<id>[a-z]+\/[A-Za-z]+)/.exec(line);
    if (audit) {
      // The message is not one line: it runs until the remedy arrow.
      const rest = [];
      for (let j = i + 1; j < lines.length; j += 1) {
        const next = lines[j];
        if (/^ {2}\[[a-z]+\]/.test(next) || /^AUDIT/.test(next) || /^──/.test(next)) break;
        if (/^\s*$/.test(next)) continue;
        rest.push(next.trim());
      }
      findings.push({ title: audit.groups.id, message: rest.join(" | ") });
    }
  }
  return findings;
}

function main(argv) {
  const level = argv.includes("--notice") ? "notice" : "error";
  const files = argv.filter((arg) => !arg.startsWith("--"));
  let printed = 0;
  for (const file of files) {
    let text = "";
    try {
      text = readFileSync(file, "utf8");
    } catch (_) {
      // A log that was never written (the step before this one died early) is not
      // an error here: the missing file is reported by the job step, not by us.
      continue;
    }
    for (const finding of extractFindings(text)) {
      const title = commandTitle(`${file}: ${finding.title}`.slice(0, 200));
      console.log(`::${level} title=${title}::${commandData(finding.message || finding.title)}`);
      printed += 1;
    }
  }
  console.log(`${printed} annotation(s) emitted`);
  return printed;
}

// This step only ever runs when a previous step failed, so finding nothing to
// report means the helper or the log is broken. Saying so with a red step is the
// point: the shell version of this script failed with a syntax error and printed
// nothing, and the run page showed only "Process completed with exit code 1".
if (main(process.argv.slice(2)) === 0) {
  console.log("::error::no failure block found in the logs — the annotation helper or the log shape needs fixing");
  process.exit(1);
}
