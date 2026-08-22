import fs from "node:fs/promises";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = "C:/Users/ISAAC/Documents/otobacktestprojesi/work/backtest";
const sourceCsv = `${root}/outputs/reports/manual_audit_package/manual_audit_trades.csv`;
const outputDir = `${root}/outputs/reports/manual_audit_package`;
const outputXlsx = `${outputDir}/manual_audit_trades.xlsx`;
const previewPng = `${outputDir}/manual_audit_preview.png`;

function csvLineCount(text) {
  return text.trimEnd().split(/\r?\n/).length;
}

function columnLetter(index1Based) {
  let n = index1Based;
  let s = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    s = String.fromCharCode(65 + r) + s;
    n = Math.floor((n - 1) / 26);
  }
  return s;
}

const csvText = await fs.readFile(sourceCsv, "utf8");
const rows = parseCsv(csvText);
const headers = rows[0];
const rowCount = rows.length;
const colCount = headers.length;
const lastCol = columnLetter(colCount);

const workbook = Workbook.create();
const audit = workbook.worksheets.add("Audit Trades");
audit.getRange(`A1:${lastCol}${rowCount}`).values = rows.map((row, rowIndex) =>
  row.map((value, colIndex) => {
    const header = headers[colIndex];
    if (rowIndex === 0) return value;
    if (["sweep_time", "cisd_time", "fvg_time", "entry_time", "exit_time"].includes(header)) {
      return formatNyTimeText(value);
    }
    if (["r_multiple", "entry_price", "stop_price", "target_price"].includes(header) && value !== "") {
      return Number(value);
    }
    return value;
  }),
);
audit.showGridLines = false;
audit.freezePanes.freezeRows(1);

const fullRange = audit.getRange(`A1:${lastCol}${rowCount}`);
const headerRange = audit.getRange(`A1:${lastCol}1`);
headerRange.format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
};
fullRange.format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
audit.getRange(`A2:${lastCol}${rowCount}`).format = {
  font: { color: "#1F2937" },
  wrapText: false,
};

const table = audit.tables.add(`A1:${lastCol}${rowCount}`, true, "ManualAuditTable");
table.showFilterButton = true;
table.style = "TableStyleMedium2";

const widths = {
  A: 12,
  B: 26,
  C: 52,
  D: 18,
  E: 20,
  F: 13,
  G: 24,
  H: 10,
  I: 12,
  J: 13,
  K: 32,
  L: 24,
  M: 24,
  N: 24,
  O: 24,
  P: 24,
  Q: 10,
  R: 10,
  S: 12,
  T: 12,
  U: 12,
  V: 28,
  W: 16,
  X: 16,
  Y: 18,
  Z: 44,
};
for (const [col, width] of Object.entries(widths)) {
  audit.getRange(`${col}:${col}`).format.columnWidth = width;
}
audit.getRange(`C2:C${rowCount}`).format.wrapText = true;
audit.getRange(`K2:K${rowCount}`).format.wrapText = true;
audit.getRange(`Z2:Z${rowCount}`).format.wrapText = true;
audit.getRange(`L2:P${rowCount}`).format.numberFormat = "@";
audit.getRange(`R2:U${rowCount}`).format.numberFormat = "0.00";

audit.getRange(`W2:W${rowCount}`).dataValidation = {
  rule: { type: "list", values: ["PASS", "FAIL", "WATCH"] },
};
audit.getRange(`X2:X${rowCount}`).dataValidation = {
  rule: { type: "list", values: ["TAKE", "SKIP"] },
};
audit.getRange(`Y2:Y${rowCount}`).dataValidation = {
  rule: { type: "list", values: ["LEVEL", "CISD", "FVG", "ENTRY", "EXIT", "CONTEXT", "OTHER"] },
};

audit.getRange(`W1:Z1`).format = {
  fill: "#7030A0",
  font: { bold: true, color: "#FFFFFF" },
  wrapText: true,
};
audit.getRange(`W2:Z${rowCount}`).format = {
  fill: "#F3E8FF",
  wrapText: true,
};

const summary = workbook.worksheets.add("Summary");
summary.showGridLines = false;
summary.getRange("A1:F1").merge();
summary.getRange("A1").values = [["Manual Audit Package"]];
summary.getRange("A1").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF", size: 16 },
};
summary.getRange("A3:B8").values = [
  ["Primary Plan", "CHALLENGE_CORE -> FON_CORE"],
  ["Total Trades", rowCount - 1],
  ["Review Status", "PASS / FAIL / WATCH"],
  ["Manual Decision", "TAKE / SKIP"],
  ["Issue Type", "LEVEL / CISD / FVG / ENTRY / EXIT / CONTEXT / OTHER"],
  ["Source CSV", sourceCsv],
];
summary.getRange("A3:A8").format = {
  fill: "#D9EAF7",
  font: { bold: true, color: "#1F2937" },
};
summary.getRange("B3:B8").format = { wrapText: true };
summary.getRange("A10:C14").values = [
  ["Bucket", "System", "Count"],
  ["challenge_failure_window", "CHALLENGE_CORE", 6],
  ["fon_core_weak_2024", "FON_CORE", 12],
  ["fon_core_flat_2025_h2", "FON_CORE", 10],
  ["fon_core_worst_month_context", "FON_CORE", 9],
];
summary.getRange("A10:C10").format = {
  fill: "#1F4E78",
  font: { bold: true, color: "#FFFFFF" },
};
summary.getRange("A10:C14").format.borders = { preset: "all", style: "thin", color: "#D9E2F3" };
summary.getRange("A:A").format.columnWidth = 28;
summary.getRange("B:B").format.columnWidth = 26;
summary.getRange("C:C").format.columnWidth = 12;

const inspect = await workbook.inspect({
  kind: "table",
  sheetId: "Audit Trades",
  range: "A1:Z8",
  include: "values",
  tableMaxRows: 8,
  tableMaxCols: 26,
  maxChars: 5000,
});
console.log(inspect.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({
  sheetName: "Audit Trades",
  range: "A1:Z12",
  scale: 1,
  format: "png",
});
await fs.writeFile(previewPng, new Uint8Array(await preview.arrayBuffer()));

await fs.mkdir(outputDir, { recursive: true });
const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputXlsx);
console.log(`Saved ${outputXlsx}`);

function parseCsv(text) {
  const parsed = [];
  let row = [];
  let field = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];
    if (inQuotes) {
      if (char === '"' && next === '"') {
        field += '"';
        i += 1;
      } else if (char === '"') {
        inQuotes = false;
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') {
      inQuotes = true;
    } else if (char === ",") {
      row.push(field);
      field = "";
    } else if (char === "\n") {
      row.push(field);
      parsed.push(row);
      row = [];
      field = "";
    } else if (char !== "\r") {
      field += char;
    }
  }
  if (field.length > 0 || row.length > 0) {
    row.push(field);
    parsed.push(row);
  }
  return parsed;
}

function formatNyTimeText(value) {
  const match = value.match(/^(\d{4}-\d{2}-\d{2})T(\d{2}:\d{2}):\d{2}([+-]\d{2}:\d{2})$/);
  if (!match) return value;
  return `${match[1]} ${match[2]} NY (${match[3]})`;
}
