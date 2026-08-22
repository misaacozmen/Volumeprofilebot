import fs from "node:fs/promises";
import path from "node:path";
import { SpreadsheetFile, Workbook } from "@oai/artifact-tool";

const root = process.cwd();
const packageDir = path.join(root, "outputs", "reports", "manual_feedback_package_v1");
const csvPath = path.join(packageDir, "manual_review_cases_simple_tr.csv");
const outputPath = path.join(packageDir, "manual_feedback_review_simple.xlsx");

function parseCsv(text) {
  const rows = [];
  let row = [];
  let value = "";
  let inQuotes = false;
  for (let i = 0; i < text.length; i += 1) {
    const char = text[i];
    const next = text[i + 1];
    if (char === '"' && inQuotes && next === '"') {
      value += '"';
      i += 1;
    } else if (char === '"') {
      inQuotes = !inQuotes;
    } else if (char === ";" && !inQuotes) {
      row.push(value);
      value = "";
    } else if ((char === "\n" || char === "\r") && !inQuotes) {
      if (char === "\r" && next === "\n") i += 1;
      row.push(value);
      if (row.some((cell) => cell !== "")) rows.push(row);
      row = [];
      value = "";
    } else {
      value += char;
    }
  }
  if (value !== "" || row.length) {
    row.push(value);
    rows.push(row);
  }
  return rows;
}

function rowsToObjects(rows) {
  const headers = rows[0].map((header) => header.replace(/^\uFEFF/, ""));
  return rows.slice(1).map((row) => Object.fromEntries(headers.map((header, index) => [header, row[index] ?? ""])));
}

function matrixFromObjects(objects, headers) {
  return [headers, ...objects.map((item) => headers.map((header) => item[header] ?? ""))];
}

function colLetter(index) {
  let n = index + 1;
  let out = "";
  while (n > 0) {
    const r = (n - 1) % 26;
    out = String.fromCharCode(65 + r) + out;
    n = Math.floor((n - 1) / 26);
  }
  return out;
}

const csvText = await fs.readFile(csvPath, "utf8");
const cases = rowsToObjects(parseCsv(csvText));

const workbook = Workbook.create();
const guide = workbook.worksheets.add("Guide");
const review = workbook.worksheets.add("Chart_Review");
const lookups = workbook.worksheets.add("Lookups");

guide.showGridLines = false;
review.showGridLines = false;
lookups.showGridLines = false;

guide.getRange("A1:H1").merge();
guide.getRange("A1").values = [["Manual Feedback Package V1"]];
guide.getRange("A1").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF", size: 16 } };

const guideRows = [
  ["Amaç", "Motorun body-FVG ve continuation sinyallerini manuel karar mantığına göre filtrelemeyi öğrenmesi."],
  ["Nasıl ilerle", "Her satırda önce chart context'e bak. Sonucu/answer key'i açmadan TAKE/SKIP/WATCH kararını ver."],
  ["En iyi yorum şekli", "Ben bu setup'ta işlem alır mıydım? Alırsam hangi yönde, hangi liquidity ve PD array yüzünden? Almazsam hangi şart bozuk?"],
  ["Öncelik", "Vaktin azsa önce body_fvg_quality, sonra same_day_or_continuation, sonra body_fvg_entry_model ve standard_fvg_control."],
  ["Answer key", "answer_key_do_not_review_first.csv ayrı dosyada duruyor. Manuel kolonları doldurmadan açma."],
];
guide.getRange("A3:B7").values = guideRows;
guide.getRange("A3:A7").format = { fill: "#D9EAF7", font: { bold: true } };
guide.getRange("A3:B7").format.borders = { preset: "all", style: "thin", color: "#B7C9D6" };
guide.getRange("A3:B7").format.wrapText = true;

const categoryRows = [
  ["review_focus", "Ne yorumlanacak?"],
  ["body_fvg_quality", "Body-FVG teknik olarak var. Asıl soru: bu PD array kaliteli mi, context doğru mu, bu işlemi manuelde alır mıydın?"],
  ["body_fvg_entry_model", "Aynı body-FVG fikrinde entry modeli/depth mantıklı mı? Midpoint mi, daha derin OTE mi, yoksa hiç trade yok mu?"],
  ["standard_fvg_control", "Mevcut standart FVG motoru manuel kararına uyuyor mu? Bunlar kontrol grubu."],
  ["same_day_or_continuation", "Aynı gün ikinci trade/continuation mantıklı mı, yoksa önceki yapı/ters sweep yüzünden skip mi?"],
];
guide.getRange("A10:B14").values = categoryRows;
guide.getRange("A10:B10").format = { fill: "#305496", font: { bold: true, color: "#FFFFFF" } };
guide.getRange("A10:B14").format.borders = { preset: "all", style: "thin", color: "#B7C9D6" };
guide.getRange("A10:B14").format.wrapText = true;

const checklistRows = [
  ["Checklist", "Sorular"],
  ["Context", "Fiyat VAH/VAL tarafında mantıklı yerde mi? Pre-market liquidity zaten alınmış mı?"],
  ["Liquidity", "Seçilen liquidity gerçekten anlamlı mı, yoksa küçük/generic swing mi?"],
  ["CISD", "Sweep sonrası yapı değişimi doğru yönde ve zamanında mı? Erken/yanlış/hallucinated CISD var mı?"],
  ["PD array", "Body-FVG/FVG/IFVG gerçekten kullanılabilir mi? Çok küçük, geç, yanlış yerde veya mitigation olmuş mu?"],
  ["Entry", "Entry fiyatı manuelde bekleyeceğin bölge mi? Stop/target mantıklı mı?"],
  ["Invalidation", "Emir beklerken ters yapı/CISD oluşmuş mu, trade iptal edilmeli mi?"],
];
guide.getRange("D10:E16").values = checklistRows;
guide.getRange("D10:E10").format = { fill: "#305496", font: { bold: true, color: "#FFFFFF" } };
guide.getRange("D10:E16").format.borders = { preset: "all", style: "thin", color: "#B7C9D6" };
guide.getRange("D10:E16").format.wrapText = true;

const reviewHeaders = Object.keys(cases[0]);
const reviewMatrix = matrixFromObjects(cases, reviewHeaders);
const lastCol = colLetter(reviewHeaders.length - 1);
const lastRow = reviewMatrix.length;
review.getRange(`A1:${lastCol}${lastRow}`).values = reviewMatrix;
review.getRange(`A1:${lastCol}1`).format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
review.getRange(`A1:${lastCol}${lastRow}`).format.borders = { preset: "inside", style: "thin", color: "#D9E2F3" };
review.getRange(`A1:${lastCol}${lastRow}`).format.wrapText = true;
review.getRange(`A1:${lastCol}${lastRow}`).format.numberFormat = "@";
review.freezePanes.freezeRows(1);
review.freezePanes.freezeColumns(2);
review.tables.add(`A1:${lastCol}${lastRow}`, true, "ManualReviewCases");

const manualCols = [
  "manual_decision",
  "manual_direction",
  "my_entry_idea",
  "my_pd_array",
  "why_take_or_skip",
  "invalidation_or_cancel",
  "manual_context_notes",
];
for (const header of manualCols) {
  const index = reviewHeaders.indexOf(header);
  if (index >= 0) {
    const col = colLetter(index);
    review.getRange(`${col}1:${col}${lastRow}`).format = { fill: "#FFF2CC" };
    review.getRange(`${col}1`).format = { fill: "#BF9000", font: { bold: true, color: "#FFFFFF" } };
  }
}

function addListValidation(header, values) {
  const index = reviewHeaders.indexOf(header);
  if (index < 0) return;
  const col = colLetter(index);
  review.getRange(`${col}2:${col}${lastRow}`).dataValidation = { rule: { type: "list", values } };
}
addListValidation("manual_decision", ["TAKE", "SKIP", "WATCH"]);
addListValidation("manual_direction", ["long", "short"]);

const widths = {
  A: 11, B: 22, C: 13, D: 24, E: 10, F: 16, G: 10, H: 10, I: 28, J: 20, K: 20, L: 20, M: 22,
  N: 16, O: 16, P: 16, Q: 16, R: 22, S: 22, T: 44, U: 44, V: 46,
};
for (const [col, width] of Object.entries(widths)) {
  review.getRange(`${col}:${col}`).format.columnWidth = width;
}

const lookupRows = [
  ["Field", "Allowed values"],
  ["manual_decision", "TAKE, SKIP, WATCH"],
  ["manual_direction", "long, short"],
  ["my_entry_idea", "Your entry level or zone"],
  ["my_pd_array", "body FVG, FVG, IFVG, OTE, none, or your own description"],
];
lookups.getRange("A1:B5").values = lookupRows;
lookups.getRange("A1:B1").format = { fill: "#1F4E78", font: { bold: true, color: "#FFFFFF" } };
lookups.getRange("A1:B5").format.borders = { preset: "all", style: "thin", color: "#B7C9D6" };
lookups.getRange("A:B").format.columnWidth = 24;

for (const sheet of [guide, review, lookups]) {
  const used = sheet.getUsedRange();
  used.format.autofitRows();
}
guide.getRange("A:A").format.columnWidth = 22;
guide.getRange("B:B").format.columnWidth = 90;
guide.getRange("D:D").format.columnWidth = 20;
guide.getRange("E:E").format.columnWidth = 82;

const inspect = await workbook.inspect({
  kind: "workbook,sheet,table",
  maxChars: 4000,
  tableMaxRows: 5,
  tableMaxCols: 8,
});
console.log(inspect.ndjson);

const errors = await workbook.inspect({
  kind: "match",
  searchTerm: "#REF!|#DIV/0!|#VALUE!|#NAME\\?|#N/A",
  options: { useRegex: true, maxResults: 100 },
  summary: "formula error scan",
});
console.log(errors.ndjson);

const preview = await workbook.render({ sheetName: "Chart_Review", range: "A1:V12", scale: 1, format: "png" });
await fs.writeFile(path.join(packageDir, "manual_feedback_review_simple_preview.png"), new Uint8Array(await preview.arrayBuffer()));

const xlsx = await SpreadsheetFile.exportXlsx(workbook);
await xlsx.save(outputPath);
console.log(`Saved ${outputPath}`);
