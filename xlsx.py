"""Tiny .xlsx writer - standard library only (runs on the GitHub runner with no extra packages).

Only what the dashboard needs: several sheets, a bold header row, inline strings, numbers with
a couple of number formats, frozen header and an autofilter.  No shared-string table, no styles
beyond the few declared below, so the file stays small and Excel/LibreOffice/Sheets all open it.
"""
import datetime as _dt
import zipfile
from xml.sax.saxutils import escape

_CT = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
%s
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
</Types>"""
_SHEET_CT = ('<Override PartName="/xl/worksheets/sheet%d.xml" '
             'ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>')
_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>
</Relationships>"""
_WB = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets>%s</sheets></workbook>"""
_WB_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">%s
<Relationship Id="rIdS" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""
_STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
<numFmts count="3"><numFmt numFmtId="164" formatCode="#,##0.00"/><numFmt numFmtId="165" formatCode="0.00&quot;%&quot;"/>
<numFmt numFmtId="166" formatCode="#,##0"/></numFmts>
<fonts count="3"><font><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="11"/><name val="Calibri"/></font>
<font><b/><sz val="12"/><name val="Calibri"/></font></fonts>
<fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="gray125"/></fill></fills>
<borders count="1"><border><left/><right/><top/><bottom/><diagonal/></border></borders>
<cellStyleXfs count="1"><xf numFmtId="0" fontId="0" fillId="0" borderId="0"/></cellStyleXfs>
<cellXfs count="6">
<xf numFmtId="0" fontId="0" fillId="0" borderId="0" xfId="0"/>
<xf numFmtId="0" fontId="1" fillId="0" borderId="0" xfId="0" applyFont="1"/>
<xf numFmtId="164" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="165" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="166" fontId="0" fillId="0" borderId="0" xfId="0" applyNumberFormat="1"/>
<xf numFmtId="0" fontId="2" fillId="0" borderId="0" xfId="0" applyFont="1"/>
</cellXfs><cellStyles count="1"><cellStyle name="Normal" xfId="0" builtinId="0"/></cellStyles>
</styleSheet>"""
_NUM, _INT, _PCT, _TXT, _HEAD = 2, 4, 3, 0, 1


def _col(i):
    s = ""
    i += 1
    while i:
        i, r = divmod(i - 1, 26)
        s = chr(65 + r) + s
    return s


def _cell(ref, v, style):
    if v is None or v == "":
        s_attr = f' s="{style}"' if style else ""
        return f'<c r="{ref}"{s_attr}/>'
    if isinstance(v, bool):
        v = "Yes" if v else "No"
    if isinstance(v, (int, float)):
        return f'<c r="{ref}" s="{style}"><v>{v}</v></c>'
    if isinstance(v, (_dt.date, _dt.datetime)):
        v = v.isoformat()[:10]
    s_attr = f' s="{style}"' if style else ""
    return f'<c r="{ref}" t="inlineStr"{s_attr}><is><t xml:space="preserve">{escape(str(v))}</t></is></c>'


def _sheet(name, rows, widths=None, freeze=True, autofilter=True):
    """rows: list of lists; the first row is written as a bold header."""
    body, ncol = [], 0
    for r, row in enumerate(rows):
        cells = []
        for c, v in enumerate(row):
            ncol = max(ncol, c + 1)
            if r == 0:
                st = _HEAD
            elif isinstance(v, float):
                st = _NUM
            elif isinstance(v, int) and not isinstance(v, bool):
                st = _INT
            else:
                st = _TXT
            cells.append(_cell(f"{_col(c)}{r + 1}", v, st))
        body.append(f'<row r="{r + 1}">{"".join(cells)}</row>')
    cols = ""
    if widths:
        cols = "<cols>" + "".join(
            f'<col min="{i+1}" max="{i+1}" width="{w}" customWidth="1"/>' for i, w in enumerate(widths)) + "</cols>"
    pane = '<sheetView workbookViewId="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView>' if freeze else '<sheetView workbookViewId="0"/>'
    af = f'<autoFilter ref="A1:{_col(max(0, ncol - 1))}{len(rows)}"/>' if autofilter and len(rows) > 1 else ""
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            f'<sheetPr/><dimension ref="A1:{_col(max(0, ncol - 1))}{len(rows)}"/>'
            f'<sheetViews>{pane}</sheetViews><sheetFormatPr defaultRowHeight="15"/>{cols}'
            f'<sheetData>{"".join(body)}</sheetData>{af}'
            '<pageMargins left="0.7" right="0.7" top="0.75" bottom="0.75" header="0.3" footer="0.3"/>'
            '</worksheet>')


def save(path, sheets):
    """sheets: list of dicts -> {name, rows, widths, freeze, autofilter, title}"""
    parts = {}
    cts, rels = [], []
    for i, sh in enumerate(sheets, 1):
        parts[f"xl/worksheets/sheet{i}.xml"] = _sheet(sh["name"], sh["rows"], sh.get("widths"),
                                                      sh.get("freeze", True), sh.get("autofilter", True))
        cts.append(_SHEET_CT % i)
        rels.append(f'<Relationship Id="rId{i}" Type="http://schemas.openxmlformats.org/officeDocument/2006/'
                    f'relationships/worksheet" Target="worksheets/sheet{i}.xml"/>')
    names = "".join(f'<sheet name="{escape(sh["name"][:31])}" sheetId="{i}" r:id="rId{i}"/>'
                    for i, sh in enumerate(sheets, 1))
    parts["[Content_Types].xml"] = _CT % "".join(cts)
    parts["_rels/.rels"] = _RELS
    parts["xl/workbook.xml"] = _WB % names
    parts["xl/_rels/workbook.xml.rels"] = _WB_RELS % "".join(rels)
    parts["xl/styles.xml"] = _STYLES
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", parts.pop("[Content_Types].xml"))
        z.writestr("_rels/.rels", parts.pop("_rels/.rels"))
        for k in ["xl/workbook.xml", "xl/_rels/workbook.xml.rels", "xl/styles.xml"] + \
                 [f"xl/worksheets/sheet{i}.xml" for i in range(1, len(sheets) + 1)]:
            z.writestr(k, parts.pop(k))
    return path
