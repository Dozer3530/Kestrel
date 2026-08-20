/*
 * Builds docs/Kestrel-User-Guide.docx.
 *
 *   node docs/build_userguide.js
 *
 * Screenshots come from docs/images (regenerate them with docs/make_screenshots.py).
 */
const fs = require("fs");
const path = require("path");
const {
  AlignmentType, Document, Footer, Header, HeadingLevel, ImageRun, LevelFormat,
  PageBreak, PageNumber, Packer, Paragraph, ShadingType, Table, TableCell, TableRow,
  TableOfContents, TextRun, WidthType, BorderStyle,
} = require("docx");

const ROOT = path.join(__dirname, "..");
const IMG = path.join(__dirname, "images");
const OUT = path.join(__dirname, "Kestrel-User-Guide.docx");

const BLUE = "1A5276";
const RUST = "C0622E";
const GREY = "5A6B78";
const RED = "C0392B";
const AMBER = "B9770E";
const GREEN = "1E8449";

const USABLE = 10080;            // page width minus margins, in DXA

/* ---------- helpers ---------- */

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}

function picture(name, maxW = 620, caption = null) {
  const file = path.join(IMG, name);
  if (!fs.existsSync(file)) return [];
  const { w, h } = pngSize(file);
  const width = Math.min(maxW, w);
  const height = Math.round((h / w) * width);
  const out = [
    new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { before: 160, after: caption ? 40 : 200 },
      children: [new ImageRun({ type: "png", data: fs.readFileSync(file),
                                transformation: { width, height } })],
    }),
  ];
  if (caption) {
    out.push(new Paragraph({
      alignment: AlignmentType.CENTER,
      spacing: { after: 220 },
      children: [new TextRun({ text: caption, italics: true, size: 18, color: GREY })],
    }));
  }
  return out;
}

const h1 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_1, text: t,
                                  spacing: { before: 360, after: 160 },
                                  pageBreakBefore: true });
const h2 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_2, text: t,
                                  spacing: { before: 280, after: 120 } });
const h3 = (t) => new Paragraph({ heading: HeadingLevel.HEADING_3, text: t,
                                  spacing: { before: 200, after: 100 } });

function p(text, opts = {}) {
  return new Paragraph({
    spacing: { after: opts.after === undefined ? 140 : opts.after },
    children: [new TextRun({ text, size: 22, ...opts })],
  });
}

/** Paragraph with mixed bold/plain runs: rich(["Bold bit", true], ["rest", false]) */
function rich(...parts) {
  return new Paragraph({
    spacing: { after: 140 },
    children: parts.map(([t, bold]) => new TextRun({ text: t, bold: !!bold, size: 22 })),
  });
}

function bullet(text, level = 0) {
  return new Paragraph({
    numbering: { reference: "bullets", level },
    spacing: { after: 70 },
    children: [new TextRun({ text, size: 22 })],
  });
}

let stepInstance = 0;
/** A numbered list that restarts at 1: steps("first", "second", ...) */
function steps(...items) {
  const instance = stepInstance++;
  return items.map((text) => new Paragraph({
    numbering: { reference: "steps", level: 0, instance },
    spacing: { after: 90 },
    children: [new TextRun({ text, size: 22 })],
  }));
}

function code(text) {
  return new Paragraph({
    spacing: { before: 60, after: 140 },
    shading: { type: ShadingType.CLEAR, fill: "F2F4F6" },
    indent: { left: 240 },
    children: [new TextRun({ text, font: "Consolas", size: 20 })],
  });
}

function callout(title, text, colour = BLUE) {
  return new Paragraph({
    spacing: { before: 140, after: 180 },
    shading: { type: ShadingType.CLEAR, fill: "EEF3F7" },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: colour, space: 8 } },
    indent: { left: 180 },
    children: [
      new TextRun({ text: title + "  ", bold: true, size: 22, color: colour }),
      new TextRun({ text, size: 22 }),
    ],
  });
}

function cell(text, { bold = false, colour = null, width, header = false } = {}) {
  return new TableCell({
    width: { size: width, type: WidthType.DXA },
    shading: header ? { type: ShadingType.CLEAR, fill: "E8EEF3" } : undefined,
    margins: { top: 60, bottom: 60, left: 110, right: 110 },
    children: [new Paragraph({
      children: [new TextRun({ text, bold: bold || header, size: 20,
                               color: colour || undefined })],
    })],
  });
}

/** table(cols, headerRow, rows, colourFn) — cols are DXA widths summing to USABLE */
function table(cols, headers, rows, colourFn = null) {
  return new Table({
    columnWidths: cols,
    width: { size: USABLE, type: WidthType.DXA },
    rows: [
      new TableRow({
        tableHeader: true,
        children: headers.map((t, i) => cell(t, { header: true, width: cols[i] })),
      }),
      ...rows.map((r) => new TableRow({
        children: r.map((t, i) => cell(String(t), {
          width: cols[i],
          colour: i === 0 && colourFn ? colourFn(r) : null,
          bold: i === 0 && !!colourFn,
        })),
      })),
    ],
  });
}

/* ---------- diagnostics reference data ---------- */

const ERRORS = [
  ["No CRS defined", "The file doesn't record which coordinate system it's in.",
   "QGIS falls back to the project CRS and can place the layer anywhere. Use Set the CRS once you know the right one."],
  ["Missing .prj (no CRS)", "A shapefile is missing its .prj sidecar.",
   "Same cause as above, specific to shapefiles. Add the .prj, or use Set the CRS to write a corrected copy."],
  ["Invalid extent", "The bounding box is empty or contains NaN.",
   "Usually empty or corrupt geometry. Re-export from the source."],
  ["Could not read file", "The file couldn't be opened at all.",
   "Check it isn't corrupt, and that it's a format Kestrel reads (see Supported formats)."],
  ["Layer could not be read", "One layer inside a multi-layer dataset failed.",
   "The rest of the dataset still loaded. That one layer may be corrupt or use an unsupported encoding."],
  ["Broken data source", "An ArcGIS layer file points at data that no longer exists.",
   "This is the red exclamation mark in ArcGIS Pro. Repoint the layer, or restore the data."],
  ["Broken link to a temporary geodatabase", "The layer pointed into ArcGIS Pro's scratch geodatabase, which has been cleaned up.",
   "The features were probably never saved anywhere permanent. Re-export from the original source."],
  ["Broken data source (it was in a temp folder)", "The source was in a Windows temp folder and has been cleared.",
   "Re-create the data somewhere permanent, then repoint the layer."],
  ["Data source could not be read", "The layer file's source exists but wouldn't open.",
   "It may be locked by another program, or need a driver Kestrel doesn't bundle."],
];

const WARNINGS = [
  ["Possible CRS / coordinate mismatch", "The declared CRS and the coordinate values disagree.",
   "The classic 'my layer is in the ocean'. Often lat/lon data tagged with a projected CRS, or vice versa."],
  ["UTM zone is the wrong hemisphere", "A northern UTM zone on southern data (or the reverse).",
   "Shifts data by thousands of kilometres. Switch to the matching N/S zone."],
  ["Eastings don't fit this UTM zone", "Easting values fall well outside the ~100,000-900,000 m a zone covers.",
   "The data was probably created in a different UTM zone than the one recorded."],
  ["Data outside the CRS's valid area", "The data sits outside the region the CRS is designed for.",
   "Usually means the wrong CRS was assigned."],
  ["Extent may wrap the antimeridian", "The extent spans most of the globe in longitude but little in latitude.",
   "Common near the International Date Line; the layer may render as a stripe across the map."],
  ["Layers use different coordinate systems", "Layers inside one dataset don't share a CRS.",
   "Unusual. Check you're loading the layer you mean, in the projection you expect."],
  ["Empty layer", "The layer has zero features.",
   "Nothing will draw. Check the export or clip that produced it."],
  ["Empty raster", "The raster has zero width or height.", "Re-export; the source may have been clipped to nothing."],
  ["Zero-area extent", "Several features share one identical location.",
   "Usually coordinates lost or overwritten on export. A single point is not flagged."],
  ["Coordinates at (0, 0) - 'Null Island'", "The data sits at longitude 0, latitude 0.",
   "Almost always means coordinates were lost or zeroed."],
  ["Invalid geometry", "Self-intersections or invalid rings were found in the sample.",
   "Breaks rendering, selection and most processing tools. Use Fix geometry."],
  ["No layers found", "The file opened but contains no readable layers.", "Confirm it actually holds vector data."],
  ["No coordinate columns found", "A CSV or spreadsheet has no recognisable coordinate columns.",
   "Rename them to something like longitude/latitude or easting/northing - or it may just be a plain table."],
  ["No overviews (pyramids)", "A large raster has no overviews.",
   "Every redraw reads full-resolution pixels, so it feels very slow zoomed out. Build overviews in QGIS or with gdaladdo."],
  ["A definition query is filtering this layer", "An ArcGIS layer is hiding features with a query.",
   "A very common reason features 'go missing'. The data is there; the layer is filtering it."],
  ["Data lives in a temporary folder", "The source is in a scratch location that gets cleaned up.",
   "It works now but will break. Move the data somewhere permanent."],
  ["Published extent is far bigger than the data", "A hosted service advertises a much larger extent than its features cover.",
   "Clients use that extent for Zoom to Layer, so it zooms to the wrong place. Update the extent in ArcGIS Online."],
  ["Features could not be read", "A service described itself but wouldn't return features.",
   "Usually means the service needs sign-in for data access."],
  ["No features returned", "A service answered a query but returned nothing.",
   "It may be empty, or filtering what anonymous users can see."],
  ["Empty point cloud", "The LAS/LAZ header reports zero points.", "The export produced nothing usable."],
  ["Flat elevation", "Every point in the cloud sits at the same height.", "Z may have been dropped on export."],
  ["Implausible elevation range", "The elevation span is impossibly large.",
   "Usually noise points, or Z recorded in different units from X and Y."],
];

const INFOS = [
  ["WGS 84 / NAD83 datum difference", "Flags that these two datums differ by roughly 1-2 m in North America.",
   "Harmless for field mapping. It matters when combining with survey, RTK or machine-guidance data."],
  ["Multiple layers", "The dataset contains more than one layer.", "Make sure you add the one you want."],
  ["3D or measured geometry", "The geometry carries Z and/or M values.", "Informational."],
  ["Mixed / unknown geometry type", "The layer can mix points, lines and polygons.",
   "Occasionally causes styling quirks in QGIS."],
  ["Attribute table (no geometry)", "A non-spatial table, normal inside a GeoPackage.", "Nothing to fix."],
  ["Coordinates found", "Kestrel located coordinate columns in a table.",
   "Use Make a point layer to turn it into real geometry."],
  ["Not internally tiled", "A large raster is stored in strips rather than tiles.",
   "Re-writing as a Cloud Optimized GeoTIFF makes panning and zooming much faster."],
  ["Uncompressed", "A large raster has no compression set.", "LZW or DEFLATE are lossless and usually shrink imagery a lot."],
  ["No NoData value set", "Nothing marks which pixels are empty.", "Blank edges draw as real values, often black."],
  ["Point density", "Points per square metre across the extent.", "Informational."],
  ["Sparse point cloud", "Fewer than about half a point per square metre.", "Thin for detailed surface work."],
  ["Elevations go below sea level", "The lowest point is well below zero.", "Fine for some datums; worth a look if unexpected."],
  ["Layer is turned off", "An ArcGIS layer is unchecked and won't draw.", "Tick it in the Contents pane."],
  ["Layer reads from a service or database", "The layer connects to a service rather than a file.",
   "Kestrel can't inspect the data itself in that case."],
  ["This service is not public", "The portal item is shared privately.", "Others will need permission in your organisation."],
  ["Service has several layers", "The service publishes more than one layer.", "Add the specific layer you want."],
  ["Showing a sample", "Kestrel capped how many features it downloaded.",
   "CRS and geometry type are accurate; treat the count and extent as a sample."],
];

const FORMATS = [
  ["Vector", "Shapefile (.shp, or zipped), GeoPackage (.gpkg), File Geodatabase (.gdb folder), GeoJSON, KML/KMZ, GML, GPX, DXF (CAD), FlatGeobuf, TopoJSON, MapInfo TAB/MIF, OSM, PMTiles, MVT, DGN, GeoPDF, ArcInfo E00"],
  ["Raster", "GeoTIFF (.tif), IMG, VRT, JPEG2000, ASCII Grid, DEM/DTED, BIL/BSQ/BIP, HDF5, netCDF, GRIB/GRIB2, ENVI, ERS, SAGA, PCIDSK, NITF, XYZ, plus PNG/JPG with a world file"],
  ["Point cloud", "LAS, LAZ (header is read, so it's instant even on very large clouds)"],
  ["Tables", "CSV, TSV, Excel .xlsx/.xlsm, and legacy Excel .xls - with coordinate columns detected automatically"],
  ["ArcGIS", "Layer files .lyrx / .mapx, portal items .pitemx, and ArcGIS REST services (FeatureServer / MapServer) by URL"],
];

/* ---------- document ---------- */

const doc = new Document({
  // Word rebuilds the table of contents when the file is opened.
  features: { updateFields: true },
  creator: "Kestrel",
  title: "Kestrel User Guide",
  description: "How to install and use Kestrel, a geospatial data sanity-checker",
  numbering: {
    config: [
      { reference: "bullets",
        levels: [
          { level: 0, format: LevelFormat.BULLET, text: "\u2022", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 620, hanging: 300 } } } },
          { level: 1, format: LevelFormat.BULLET, text: "\u25E6", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 1080, hanging: 300 } } } },
        ] },
      { reference: "steps",
        levels: [
          { level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
            style: { paragraph: { indent: { left: 620, hanging: 300 } } } },
        ] },
    ],
  },
  styles: {
    default: {
      document: { run: { font: "Calibri", size: 22 } },
      heading1: { run: { font: "Calibri Light", size: 36, bold: true, color: BLUE } },
      heading2: { run: { font: "Calibri Light", size: 28, bold: true, color: BLUE } },
      heading3: { run: { font: "Calibri", size: 24, bold: true, color: "34495E" } },
    },
  },
  sections: [{
    properties: {
      page: { size: { width: 12240, height: 15840 },
              margin: { top: 1080, right: 1080, bottom: 1080, left: 1080 } },
    },
    footers: {
      default: new Footer({
        children: [new Paragraph({
          alignment: AlignmentType.CENTER,
          children: [new TextRun({ text: "Kestrel User Guide  \u00B7  ", size: 18, color: GREY }),
                     new TextRun({ children: [PageNumber.CURRENT], size: 18, color: GREY })],
        })],
      }),
    },
    children: [
      /* ===== cover ===== */
      ...(fs.existsSync(path.join(IMG, "logo.png"))
        ? [new Paragraph({ alignment: AlignmentType.CENTER, spacing: { before: 1400, after: 200 },
            children: [new ImageRun({ type: "png", data: fs.readFileSync(path.join(IMG, "logo.png")),
                                      transformation: { width: 150, height: 150 } })] })]
        : [new Paragraph({ spacing: { before: 2200 } })]),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 80 },
        children: [new TextRun({ text: "Kestrel", bold: true, size: 84, color: BLUE, font: "Calibri Light" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 500 },
        children: [new TextRun({ text: "A sharp eye on your geospatial data", size: 30, color: GREY, italics: true })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 120 },
        children: [new TextRun({ text: "User Guide", bold: true, size: 44, color: "34495E" })] }),
      new Paragraph({ alignment: AlignmentType.CENTER, spacing: { after: 1600 },
        children: [new TextRun({ text: "Version 0.8.0", size: 24, color: GREY })] }),
      new Paragraph({ alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "github.com/Dozer3530/Kestrel", size: 22, color: BLUE }),
                   new PageBreak()] }),

      /* ===== contents ===== */
      new Paragraph({ heading: HeadingLevel.HEADING_1, text: "Contents", spacing: { after: 200 } }),
      new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
      new Paragraph({
        spacing: { before: 240 },
        children: [new TextRun({
          text: "If this page looks empty, press Ctrl+A then F9 to build the contents list.",
          italics: true, size: 18, color: GREY })],
      }),
      new Paragraph({ children: [new PageBreak()] }),

      /* ===== 1 about ===== */
      h1("1. About Kestrel"),
      p("Kestrel is a small Windows desktop program that answers one question quickly: what is wrong with this geospatial file? You drop a file on it and get back the coordinate system, where the data actually sits on Earth, and a plain-English list of anything that would stop it drawing correctly in QGIS or ArcGIS."),
      p("It was built for the moment you drag a layer into QGIS and nothing appears \u2014 no error, no shapes, just empty space. Instead of opening the file in three different tools to work out why, you drop it on Kestrel and it tells you."),
      h2("1.1 What it does"),
      bullet("Reads the coordinate system, UTM zone, units and datum of almost any geospatial file."),
      bullet("Shows where the data really is, on a globe and on a satellite map."),
      bullet("Runs around 48 checks for the things that commonly go wrong."),
      bullet("Repairs the common problems \u2014 assigning a CRS, reprojecting, fixing geometry, converting format."),
      bullet("Audits an entire folder at once and exports the result as CSV or HTML."),
      bullet("Follows ArcGIS layer files and hosted services to the data behind them."),
      h2("1.2 Who it is for"),
      p("Anyone who receives geospatial data from other people: GIS analysts, agronomists, surveyors, technicians and data managers. No programming is needed. There is also a command line for anyone who wants to build Kestrel into a scripted process."),
      h2("1.3 What Kestrel never does"),
      callout("Your files are safe.",
        "Kestrel only ever opens your data for reading. Every repair writes a brand-new file into a folder you choose; the original is never modified, moved or deleted. If a repair fails part-way, nothing is left behind.", GREEN),

      /* ===== 2 install ===== */
      h1("2. Installing Kestrel"),
      h2("2.1 What you need"),
      bullet("Windows 10 or Windows 11, 64-bit."),
      bullet("About 400 MB of free disk space."),
      bullet("No Python, no QGIS, no ArcGIS licence \u2014 everything is bundled."),
      bullet("An internet connection is optional. It is only used for satellite imagery and for reading online services; every other feature works offline."),
      h2("2.2 Installing (recommended)"),
      ...steps(
        "Go to github.com/Dozer3530/Kestrel/releases and open the latest release.",
        "Download KestrelSetup.exe.",
        "Run it. No administrator rights are required \u2014 it installs for your user account only, which means it works on locked-down work computers.",
        "Leave 'Create a desktop shortcut' ticked if you want one, then click Install.",
      ),
      p("The installer adds a Start Menu entry, an uninstaller, and a right-click 'Inspect with Kestrel' option for geospatial files in File Explorer."),
      h2("2.3 The portable version"),
      p("If you cannot run installers, download Kestrel-windows.zip instead, unzip it anywhere (including a USB drive or a network folder), and run Kestrel\\Kestrel.exe."),
      callout("Keep the folder together.",
        "The portable version needs the _internal folder that sits beside Kestrel.exe. Moving the .exe on its own will not work.", AMBER),
      h2("2.4 The Windows security warning"),
      p("The first time you run Kestrel, Windows may show a blue box saying 'Windows protected your PC'. This appears because the program is not code-signed, not because anything is wrong with it. Click 'More info', then 'Run anyway'. You will only see this once."),
      h2("2.5 Updating"),
      p("Download the newer KestrelSetup.exe and run it. It upgrades in place and keeps your settings, including your chosen output folder. There is no need to uninstall first."),
      h2("2.6 Uninstalling"),
      p("Settings \u203A Apps \u203A Installed apps \u203A Kestrel \u203A Uninstall. This also removes the right-click menu entry. Any files Kestrel wrote for you are left alone."),

      /* ===== 3 quick start ===== */
      h1("3. Quick start"),
      p("If you only read one page, read this one."),
      ...steps(
        "Open Kestrel from the Start Menu.",
        "Drag a geospatial file onto the window.",
        "Read the top card: that is the coordinate system.",
        "Look at the map: that is where the data actually is.",
        "Read the Diagnostics card: red means it is broken, orange means be careful, and nothing there means it is fine.",
        "If there is a red or orange item with a Fix button, click it. Kestrel will write a corrected copy \u2014 your original file is untouched.",
      ),
      p("To check many files at once, click Audit folder\u2026 and pick a folder."),

      /* ===== 4 window tour ===== */
      h1("4. The main window"),
      ...picture("01-empty.png", 620, "Kestrel when it first opens."),
      h2("4.1 The toolbar"),
      table([2400, 7680], ["Button", "What it does"], [
        ["Browse\u2026", "Open one file using a normal file dialog."],
        ["Audit folder\u2026", "Check every geospatial file in a folder at once."],
        ["Open URL\u2026", "Inspect an ArcGIS REST service by pasting its address."],
        ["Fix / Convert", "Repair or convert the file currently open. Writes a new file; never changes the original."],
        ["Output folder", "Choose where repaired files are written. Remembered between sessions."],
        ["Copy report", "Copy the whole report as plain text, ready to paste into an email or ticket."],
        ["Open folder", "Open the folder containing the current file in File Explorer."],
      ]),
      h2("4.2 The drop zone"),
      p("The large dashed area accepts anything you drag onto it: a single file, or a folder. Dropping a folder starts a folder audit. Dropping a File Geodatabase (.gdb) folder inspects it as a dataset."),

      /* ===== 5 one file ===== */
      h1("5. Checking a single file"),
      h2("5.1 Four ways to open a file"),
      bullet("Drag the file onto the Kestrel window."),
      bullet("Click Browse\u2026 and pick it."),
      bullet("Right-click the file in File Explorer and choose 'Inspect with Kestrel'. On Windows 11 this is under 'Show more options'."),
      bullet("Drag the file onto Kestrel.exe, or pass it on the command line."),
      h2("5.2 Reading the report"),
      p("The report is a stack of cards. Problems always appear at the top, so if something is wrong you see it first."),
      ...picture("02-report.png", 560, "A clean file: coordinate system, location, map, details and diagnostics."),
      h3("Coordinate System (CRS)"),
      p("The most important card, and the reason Kestrel exists. It shows the CRS name, the EPSG code, the UTM zone where relevant, whether the CRS is projected or geographic, the measurement units, the datum, and the region the CRS is intended for."),
      p("If this card is red and says Missing, the file does not record its coordinate system at all. That is the single most common reason a layer appears in the wrong place, or does not appear."),
      h3("Location (WGS84 lon/lat)"),
      p("The data's extent converted to ordinary longitude and latitude, so you can sanity-check it against somewhere you recognise. If this says the location is unavailable, it is usually because there is no CRS to convert from."),
      h3("On the map"),
      p("Covered in detail in section 6."),
      h3("Details"),
      p("Geometry type, feature count, field names and the extent in the file's own coordinates. For rasters you get pixel dimensions, band count, data type, pixel size and the NoData value instead."),
      h3("Diagnostics"),
      p("Everything Kestrel thinks is worth telling you, with an explanation and a suggested fix. Section 7 lists every check."),
      h2("5.3 Sharing a report"),
      p("Copy report puts the entire report on the clipboard as plain text. It is the quickest way to tell a colleague or a data provider exactly what is wrong with a file."),

      /* ===== 6 map ===== */
      h1("6. The map"),
      p("The map card answers 'is this in the right place?' at a glance, using two panes."),
      h2("6.1 The globe"),
      p("On the left, a globe centred on your data, so you can immediately see which continent it landed on. If a file is supposed to be in Alberta and the marker is in the Indian Ocean, you have found your problem without reading a single number."),
      h2("6.2 The detail view"),
      p("On the right, a zoomed view of the data's own extent. The caption underneath tells you how far across the data is and names the nearest town, for example 'Data is 0.8 km across \u2014 75 km N of Calgary'."),
      h2("6.3 Satellite imagery"),
      p("The Satellite tick-box under the map switches between real aerial imagery and a plain offline map. Imagery is on by default and comes from Esri World Imagery. No account or API key is needed."),
      bullet("With imagery on, you can zoom right in and see the actual field."),
      bullet("With imagery off, you get coastlines, lakes, borders and town names \u2014 drawn from data bundled inside Kestrel, so it works with no internet at all."),
      bullet("If imagery cannot be reached, Kestrel quietly falls back to the offline map and says so. Nothing hangs."),
      callout("Working offline?",
        "Untick Satellite. Everything else in Kestrel \u2014 every check, every repair, the folder audit \u2014 works with no network connection.", BLUE),

      /* ===== 7 diagnostics ===== */
      h1("7. Diagnostics reference"),
      p("Kestrel runs around 48 checks. Each one is reported with a severity, an explanation of what it found, and a suggested fix."),
      h2("7.1 Severity levels"),
      table([1900, 8180], ["Severity", "Meaning"], [
        ["ERROR", "The data is broken or will not draw. Deal with this before using the file."],
        ["WARNING", "The data will load but something looks wrong. Worth checking before you trust it."],
        ["INFO", "Context, not a problem. Useful to know; nothing to fix."],
      ], (r) => (r[0] === "ERROR" ? RED : r[0] === "WARNING" ? AMBER : BLUE)),
      p("In a folder audit, only errors and warnings count against a file. Info notes never make a file 'not clean'."),
      h2("7.2 Errors"),
      table([3000, 3400, 3680], ["Check", "What it means", "What to do"], ERRORS),
      h2("7.3 Warnings"),
      table([3000, 3400, 3680], ["Check", "What it means", "What to do"], WARNINGS),
      h2("7.4 Information"),
      table([3000, 3400, 3680], ["Note", "What it means", "What to do"], INFOS),

      /* ===== 8 repairs ===== */
      h1("8. Fixing problems"),
      h2("8.1 How repairs work"),
      p("Kestrel can repair the most common problems it finds. Every repair follows the same rules:"),
      bullet("Your original file is opened for reading only. It is never modified."),
      bullet("The result is written as a new file, into an output folder you choose."),
      bullet("Kestrel shows you exactly what it is about to do, and you confirm before anything is written."),
      bullet("Files are written safely: if a repair fails part-way, no half-written file is left behind."),
      bullet("An earlier repair is never overwritten \u2014 a number is added to the name instead."),
      bullet("Afterwards, Kestrel re-opens the file it just wrote and reports what it actually produced."),
      h2("8.2 Choosing an output folder"),
      p("The first time you run a repair, Kestrel asks where to put repaired files. Pick somewhere sensible \u2014 a 'fixed' or 'cleaned' folder works well. It is remembered from then on, and you can change it any time with the Output folder button."),
      h2("8.3 Setting the CRS"),
      p("This is the fix for 'No CRS defined' and 'Missing .prj'. It tags the file with the coordinate system it is really in. It does not move any coordinates \u2014 it only records what they already mean."),
      ...picture("03-problems.png", 560, "A file with no CRS. The fix appears on the diagnostic itself."),
      p("Click Set the CRS\u2026 and Kestrel offers a ranked list of candidates."),
      ...picture("07-crspicker.png", 520, "Choosing a coordinate system."),
      h3("How the suggestions work"),
      p("Coordinates alone cannot identify a UTM zone \u2014 the same easting and northing is a valid location in all sixty of them. So Kestrel uses context instead:"),
      bullet("Other files in the same folder that do declare a CRS. This is why the common case \u2014 one shapefile in a delivery lost its .prj \u2014 usually gets the right answer with high confidence."),
      bullet("Coordinate systems you have chosen before."),
      bullet("The latitude implied by the northing, which narrows the candidates."),
      p("Each suggestion shows a confidence level and the reason it was suggested. If none of them are right, type an EPSG code directly, or search the EPSG database by name (for example 'NAD83 UTM 11')."),
      callout("Choose carefully.",
        "Kestrel cannot know which CRS is correct \u2014 only which are plausible. If in doubt, ask whoever produced the data. Assigning the wrong CRS puts the data confidently in the wrong place.", AMBER),
      h2("8.4 Reprojecting"),
      p("Fix / Convert \u203A Reproject to\u2026 transforms the coordinates into a different CRS and writes a new file. Use this when you need data to match a project in another coordinate system. Unlike Set the CRS, this really does move the coordinates."),
      p("A file with no CRS cannot be reprojected \u2014 there is nothing to convert from. Set its CRS first."),
      h2("8.5 Fixing geometry"),
      p("Repairs self-intersections and invalid rings. Invalid geometry is a common cause of processing tools failing with confusing errors. Kestrel only offers this when it has actually found invalid geometry."),
      p("Repairing a self-intersecting shape can split it into several parts. That is normal and is what makes it valid."),
      h2("8.6 Converting format"),
      p("Fix / Convert offers GeoPackage, Shapefile and GeoJSON."),
      bullet("GeoPackage is the best general choice: no field-name limits, no 2 GB cap, one tidy file."),
      bullet("Shapefile is for handing data to older software. Kestrel warns you about the limits first \u2014 field names shortened to 10 characters, text truncated at 254, and dates becoming text."),
      bullet("GeoJSON is for web maps and sharing. Output is always reprojected to EPSG:4326, as the format expects."),
      h2("8.7 Turning a table into a point layer"),
      p("When Kestrel finds coordinate columns in a CSV or spreadsheet, Make a point layer\u2026 turns it into a real spatial layer. You choose the CRS the coordinates are in \u2014 usually EPSG:4326 for longitude and latitude."),
      p("Rows whose coordinates cannot be read are skipped, and Kestrel tells you how many."),
      h2("8.8 What cannot be repaired"),
      p("Kestrel refuses these up front, with a reason, rather than failing halfway:"),
      bullet("Point clouds (LAS/LAZ) cannot be rewritten. Use lasinfo or PDAL, or re-export from the software that made the file."),
      bullet("Rasters cannot have a CRS assigned or geometry fixed \u2014 those are vector operations."),
      bullet("A layer file whose data source is missing has nothing to convert."),
      bullet("A service already declares its CRS, so use Reproject or Convert instead of Set the CRS."),
      callout("Multi-layer datasets.",
        "If a dataset contains several layers, a repair writes the first one only. Kestrel names the layers it will not include in the preview, so check that message before you confirm.", AMBER),

      /* ===== 9 batch ===== */
      h1("9. Auditing a whole folder"),
      p("This is the feature that saves the most time. Instead of checking files one at a time, point Kestrel at a folder and it checks everything inside."),
      h2("9.1 Running an audit"),
      p("Click Audit folder\u2026 and choose a folder, or simply drag a folder onto the window. Kestrel searches the folder and everything beneath it, then checks each dataset it finds. A progress window shows what it is doing and lets you cancel at any point."),
      ...picture("06-batch.png", 620, "A folder audit. Problems are sorted to the top."),
      h2("9.2 Reading the results"),
      table([2100, 7980], ["Column", "Meaning"], [
        ["File", "Path relative to the folder you audited."],
        ["Type", "vector, raster, pointcloud, layerfile, service or table."],
        ["CRS", "The EPSG code, or 'none' if the file does not declare one."],
        ["Size", "Feature count for vector data, or pixel dimensions for rasters."],
        ["Issues", "Errors and warnings found, or 'ok'."],
      ]),
      bullet("Red rows have errors. Orange rows have warnings. Plain rows are clean."),
      bullet("Click any column heading to sort by it."),
      bullet("Double-click a row to open that file in the normal detailed view."),
      h2("9.3 Exporting the results"),
      p("Export CSV\u2026 gives you a spreadsheet, useful for tracking what still needs fixing. Export HTML\u2026 gives you a formatted report you can email or attach to a delivery as evidence the data was checked."),
      h2("9.4 Things to know"),
      bullet("A File Geodatabase counts as one dataset, not as its many internal files."),
      bullet("Shapefile sidecars (.shx, .dbf, .prj) do not each become a row."),
      bullet("Plain images, PDFs and .json files are skipped during a scan, but still open normally if you point at one directly."),
      bullet("An audit stops at 2,000 datasets and tells you if it hit that limit."),
      bullet("Auditing a large folder on a network drive can take several minutes. The progress window keeps you informed."),

      /* ===== 10 arcgis ===== */
      h1("10. Working with ArcGIS data"),
      h2("10.1 Layer files (.lyrx, .mapx, .pitemx)"),
      p("An ArcGIS layer file does not contain data \u2014 it points at data somewhere else. So Kestrel follows the pointer and reports on what it finds."),
      ...picture("05-lyrx.png", 600, "A layer file whose data has been deleted."),
      p("The checks that matter for layer files are:"),
      bullet("Is the data still there? A broken link is the red exclamation mark in ArcGIS Pro."),
      bullet("Is it pointing at a scratch geodatabase? ArcGIS Pro saves new features into a temporary Default.gdb unless told otherwise, and Windows later deletes that folder. The layer works today and is broken next week. Kestrel calls this out specifically."),
      bullet("Is a definition query hiding features? The data is there; the layer is filtering it."),
      bullet("Is the layer simply switched off?"),
      p("When the source resolves, Kestrel inspects it normally, so you still get the real CRS, extent and map of the data behind the layer."),
      p("ArcMap's older binary .lyr format is not supported \u2014 it is undocumented and needs Esri's own libraries to read."),
      h2("10.2 Hosted services"),
      p("Click Open URL\u2026 and paste a FeatureServer or MapServer address. Add a layer number on the end (for example .../FeatureServer/0) to inspect one specific layer."),
      p("Kestrel reads the service like any other layer: CRS, location, geometry type, fields, and a map of the actual features. Nothing is uploaded \u2014 it only requests data from the address you give it."),
      h3("The published extent check"),
      p("Kestrel compares the extent a service advertises against where its features actually are. This matters because every client uses the published extent for Zoom to Layer. A stale extent sends everyone to the wrong place, and nothing in the ArcGIS interface warns you about it."),
      h3("Exporting a service"),
      p("With a service open, Fix / Convert \u203A Convert to\u2026 downloads the features into a local GeoPackage, Shapefile or GeoJSON. Up to 5,000 features are retrieved."),
      callout("Secured services.",
        "Kestrel has no sign-in. It reads whatever a service allows anonymously. If a private service reports that features could not be read, that is why.", BLUE),

      /* ===== 11 point clouds ===== */
      h1("11. Point clouds"),
      p("Kestrel reads LAS and LAZ files \u2014 lidar and drone photogrammetry output. Only the file header is read, so even a multi-gigabyte cloud opens instantly."),
      ...picture("04-pointcloud.png", 560, "A LAS file from a drone survey."),
      p("You get the coordinate system, the number of points, the extent, the elevation range, the point density and the return counts."),
      p("The LAS format makes the coordinate system optional, and exports from drone and scanner software leave it out constantly. Kestrel treats a missing CRS here as an error, exactly as it does for a shapefile with no .prj."),
      p("Kestrel also flags empty clouds, flat elevation (Z dropped on export), and impossible elevation ranges, which usually mean noise points or Z recorded in different units from X and Y."),
      p("Point clouds are read-only in Kestrel \u2014 it will not rewrite them."),

      /* ===== 12 formats ===== */
      h1("12. Supported formats"),
      table([1900, 8180], ["Category", "Formats"], FORMATS),
      p("Unknown file extensions are tried as vector data first and then as raster, so unusual formats often work anyway."),
      p("ECW and MrSID files cannot be read: they need a licensed driver that cannot be redistributed. Kestrel tells you this clearly and suggests converting to GeoTIFF in QGIS."),

      /* ===== 13 CLI ===== */
      h1("13. Using Kestrel from the command line"),
      p("Everything the window does is also available from a command prompt, which makes Kestrel usable inside scripts and automated checks. In an installed copy, the program is at:"),
      code("%LOCALAPPDATA%\\Programs\\Kestrel\\Kestrel.exe"),
      h2("13.1 Checking one file"),
      code("Kestrel.exe \"C:\\data\\fields.gpkg\""),
      p("That opens the window with the file already loaded. For text output, use the Python version of the tool from the source repository:"),
      code("py cli.py \"C:\\data\\fields.gpkg\""),
      h2("13.2 Machine-readable output"),
      code("py cli.py --json \"C:\\data\\fields.gpkg\""),
      p("Produces JSON containing the CRS, location, layers, fields and every diagnostic \u2014 suitable for feeding into another program."),
      h2("13.3 Auditing a folder"),
      code("py cli.py --batch \"C:\\deliveries\\2026-08\" --csv audit.csv --html audit.html"),
      p("Other options:"),
      table([2600, 7480], ["Option", "Effect"], [
        ["--csv <file>", "Write the results as a spreadsheet."],
        ["--html <file>", "Write a formatted report."],
        ["--no-recurse", "Only check the top-level folder, not sub-folders."],
      ]),
      h2("13.4 Exit codes"),
      table([1600, 8480], ["Code", "Meaning"], [
        ["0", "Everything checked out, or only warnings and notes were found."],
        ["1", "At least one error was found, or a file could not be read."],
        ["2", "Kestrel was used incorrectly \u2014 for example a missing option value."],
      ]),
      p("This makes it straightforward to fail an automated check when a delivery contains broken data."),

      /* ===== 14 explorer ===== */
      h1("14. File Explorer integration"),
      p("The installer adds an 'Inspect with Kestrel' entry to the right-click menu for common geospatial file types. On Windows 11 you may need to click 'Show more options' first."),
      p("If you use the portable version and want the same thing, run this once:"),
      code("Kestrel.exe --register"),
      p("To remove it again:"),
      code("Kestrel.exe --unregister"),
      p("This only changes settings for your own user account and needs no administrator rights."),

      /* ===== 15 troubleshooting ===== */
      h1("15. Troubleshooting"),
      table([3400, 6680], ["Problem", "What to do"], [
        ["Windows blocks the program on first run",
         "Click 'More info' then 'Run anyway'. The program is unsigned, which is why the warning appears."],
        ["A file on a network drive will not open",
         "Kestrel handles network paths, including awkward file names. If it still fails, copy the file locally and try again \u2014 that will show whether it is a permissions problem."],
        ["The satellite map is blank",
         "Either there is no internet connection, or a firewall is blocking it. Untick Satellite to use the offline map. Everything else still works."],
        ["A folder audit is slow",
         "Auditing across a network drive is much slower than a local folder. The progress window shows what it is doing, and you can cancel at any time."],
        ["'ECW files need a licensed driver'",
         "That format cannot be included for licensing reasons. Open the file in QGIS and export it as a GeoTIFF."],
        ["A repair button is missing",
         "Repairs are only offered where they make sense. Point clouds cannot be rewritten, and rasters cannot have geometry fixed."],
        ["Kestrel says a layer file's data is missing",
         "That is the same problem ArcGIS Pro shows as a red exclamation mark. Repoint the layer to where the data now lives."],
        ["The results look like the previous file",
         "Not possible in current versions: a failed inspection clears the previous report. If you see this, please report it."],
      ]),

      /* ===== 16 glossary ===== */
      h1("16. Glossary"),
      table([2400, 7680], ["Term", "Meaning"], [
        ["CRS", "Coordinate Reference System. The definition of what the numbers in a file mean as real positions on Earth."],
        ["EPSG code", "A short number identifying a CRS, for example EPSG:26911. The quickest unambiguous way to name one."],
        ["UTM zone", "One of 60 north-south strips used by the UTM projection. Data in the wrong zone lands hundreds of kilometres sideways."],
        ["Datum", "The model of the Earth's shape a CRS is built on. WGS 84 and NAD83 differ by 1-2 m in North America."],
        ["Projected CRS", "Coordinates in metres or feet on a flat grid, for example UTM."],
        ["Geographic CRS", "Coordinates in degrees of longitude and latitude, for example EPSG:4326."],
        ["Extent", "The bounding box containing all of a dataset's features."],
        [".prj file", "The small sidecar file that records a shapefile's CRS. Without it the shapefile has no coordinate system."],
        ["Overviews", "Lower-resolution copies stored inside a raster so it draws quickly when zoomed out. Also called pyramids."],
        ["NoData", "The pixel value that means 'nothing here', used for padding around raster data."],
        ["Definition query", "A filter on an ArcGIS layer that hides features which do not match it."],
        ["Scratch geodatabase", "ArcGIS Pro's temporary Default.gdb. Windows deletes it, taking any data saved there with it."],
      ]),

      /* ===== help ===== */
      h1("17. Getting help"),
      p("Kestrel is free and open source under the MIT licence."),
      table([2600, 7480], ["Resource", "Where"], [
        ["Downloads", "github.com/Dozer3530/Kestrel/releases"],
        ["Website", "dozer3530.github.io/Kestrel"],
        ["Source code", "github.com/Dozer3530/Kestrel"],
        ["Report a problem", "github.com/Dozer3530/Kestrel/issues"],
      ]),
      h2("17.1 Reporting a problem well"),
      p("The most useful bug report includes:"),
      bullet("What you dropped on Kestrel (the file type, and roughly how big)."),
      bullet("What you expected, and what happened instead."),
      bullet("The output of Copy report, if the file opened at all."),
      bullet("The version, from the title of the release you installed."),
      p("If the file itself can be shared, that is the single most helpful thing \u2014 most problems are specific to one file's contents."),
      new Paragraph({ spacing: { before: 500 }, alignment: AlignmentType.CENTER,
        children: [new TextRun({ text: "\u2014 end of guide \u2014", italics: true, color: GREY, size: 20 })] }),
    ],
  }],
});

Packer.toBuffer(doc).then((buf) => {
  fs.writeFileSync(OUT, buf);
  console.log("wrote", OUT, (buf.length / 1024).toFixed(0) + " KB");
});
